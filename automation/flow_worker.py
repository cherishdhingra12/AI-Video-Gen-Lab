"""
flow_worker.py — Autonomous generator for Google Flow clips via the live authenticated Chrome tab.
Drives prompt insertion, generation, progress polling, download capture, and SQLite status updates.
"""

import os
import sys
import time
import json
import glob
import shutil
import sqlite3
import subprocess

_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _DIR)
sys.path.insert(0, ROOT)

from state import State

def exec_js(js_code: str) -> str:
    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"')
    apple_script = f'''
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                if (URL of t) contains "project/" then
                    return execute t javascript "{escaped_js}"
                end if
            end repeat
        end repeat
    end tell
    '''
    res = subprocess.run(['osascript', '-e', apple_script], capture_output=True, text=True)
    return res.stdout.strip()

def ensure_in_project():
    res = exec_js('location.href')
    if not res or 'project/' not in res:
        raise RuntimeError(f"Chrome tab is not inside a Flow project: {res}")
    return res

def dismiss_modals():
    js = '''(function(){
        const closeBtns = Array.from(document.querySelectorAll("button")).filter(b => 
            (b.innerText||"").trim().toLowerCase() === "close" || 
            (b.getAttribute("aria-label")||"").toLowerCase().includes("close")
        );
        closeBtns.forEach(b => b.click());
        return closeBtns.length;
    })()'''
    return exec_js(js)

def get_posters() -> list:
    js = '''(function(){
        const posters = Array.from(document.querySelectorAll("img")).filter(im => 
            (im.src && im.src.includes("flow-content.google")) || (im.height > 150 && im.height > im.width)
        );
        return JSON.stringify(posters.map(p => p.src));
    })()'''
    raw = exec_js(js)
    try:
        return json.loads(raw)
    except Exception:
        return []

def submit_prompt(prompt: str) -> bool:
    dismiss_modals()
    js_type = f'''(function(){{
        const pm = document.querySelector("div.ProseMirror");
        if (!pm) return "no ProseMirror";
        pm.focus();
        document.execCommand("selectAll", false, null);
        document.execCommand("delete", false, null);
        document.execCommand("insertText", false, {json.dumps(prompt)});
        return "typed";
    }})()'''
    res = exec_js(js_type)
    if "typed" not in res:
        raise RuntimeError(f"Failed to type prompt: {res}")
    
    time.sleep(1.2)
    js_click = '''(function(){
        const btn = document.querySelector(".generate-icon-button");
        if (btn && !btn.disabled) {
            btn.click();
            return "clicked";
        }
        return btn ? ("btn disabled: " + btn.disabled) : "no btn";
    })()'''
    res = exec_js(js_click)
    if "clicked" not in res:
        raise RuntimeError(f"Failed to click generate button: {res}")
    return True

def poll_render(initial_posters: set, timeout: float = 180.0) -> str:
    start = time.time()
    last_pct = ""
    started = False
    
    # Wait up to 15s for generation to be picked up
    while time.time() - start < timeout:
        js = '''(function(){
            const body = document.body.innerText;
            const pct = body.match(/\\d+%/g);
            const hasError = /Sorry, this video failed to generate/i.test(body);
            return JSON.stringify({
                pct: pct ? pct[0] : null,
                hasError: hasError
            });
        })()'''
        info_raw = exec_js(js)
        try:
            info = json.loads(info_raw)
        except Exception:
            info = {}

        if info.get("hasError"):
            raise RuntimeError("Flow reported: Sorry, this video failed to generate.")

        pct = info.get("pct")
        if pct:
            started = True
            if pct != last_pct:
                print(f"    progress: {pct}")
                last_pct = pct

        current_posters = set(get_posters())
        diff = current_posters - initial_posters

        if started and not pct and len(diff) > 0:
            new_poster_src = list(diff)[0]
            print(f"    render completed! New poster: {new_poster_src[:60]}...")
            return new_poster_src

        time.sleep(3)
        
    raise TimeoutError(f"Flow render timed out after {timeout}s")

def download_poster(poster_src: str, dest: str, timeout: float = 60.0) -> str:
    dl_dir = os.path.expanduser('~/Downloads')
    before = set(os.listdir(dl_dir))

    # Click the specific new poster
    js_click_poster = f'''(function(){{
        const poster = Array.from(document.querySelectorAll("img")).find(im => 
            im.src === {json.dumps(poster_src)}
        );
        if (poster) {{
            poster.click();
            return "clicked poster";
        }}
        // Fallback to first portrait poster
        const fallback = Array.from(document.querySelectorAll("img")).find(im => 
            im.height > 150 && im.height > im.width
        );
        if (fallback) {{
            fallback.click();
            return "clicked fallback";
        }}
        return "no poster";
    }})()'''
    res = exec_js(js_click_poster)
    if "clicked" not in res:
        raise RuntimeError(f"Could not click poster: {res}")

    time.sleep(2.5)

    # Click download button (either direct or inside More options)
    js_click_dl = '''(function(){
        const dl = Array.from(document.querySelectorAll("button")).find(b => 
            (b.innerText||"").toLowerCase().includes("download") || 
            (b.getAttribute("aria-label")||"").toLowerCase().includes("download")
        );
        if (dl) {
            dl.click();
            return "clicked dl";
        }
        const more = Array.from(document.querySelectorAll("button")).find(b => 
            (b.getAttribute("aria-label")||"").toLowerCase().includes("more options")
        );
        if (more) {
            more.click();
            return "clicked more";
        }
        return "no dl";
    })()'''
    dl_res = exec_js(js_click_dl)
    time.sleep(1.2)
    if "clicked more" in dl_res:
        js_menu_dl = '''(function(){
            const item = Array.from(document.querySelectorAll("[role=menuitem], .mat-mdc-menu-item, button")).find(b => 
                (b.innerText||"").toLowerCase().includes("download")
            );
            if (item) {
                item.click();
                return "clicked menu dl";
            }
            return "no menu dl";
        })()'''
        exec_js(js_menu_dl)
        time.sleep(1.2)

    # Click 720p Original size
    js_click_orig = '''(function(){
        const item = Array.from(document.querySelectorAll(".mat-mdc-menu-item, [role=menuitem], button")).find(b => 
            (b.innerText||"").includes("Original size") || (b.innerText||"").includes("720p")
        );
        if (item) {
            item.click();
            return "clicked orig";
        }
        return "no orig";
    })()'''
    exec_js(js_click_orig)

    # Wait for file in ~/Downloads
    start = time.time()
    downloaded_file = None
    while time.time() - start < timeout:
        after = set(os.listdir(dl_dir))
        diff = after - before
        candidates = [f for f in diff if f.endswith('.mp4')]
        if candidates:
            cand_path = os.path.join(dl_dir, candidates[0])
            s1 = os.path.getsize(cand_path)
            time.sleep(1)
            s2 = os.path.getsize(cand_path)
            if s1 == s2 and s1 > 1000:
                downloaded_file = cand_path
                break
        time.sleep(1)

    # Return back to project view
    js_back = '''(function(){
        const back = Array.from(document.querySelectorAll("button")).find(b => 
            (b.innerText||"").includes("arrow_back") || 
            (b.getAttribute("aria-label")||"").toLowerCase().includes("back")
        );
        if (back) back.click();
    })()'''
    exec_js(js_back)
    time.sleep(2)

    if not downloaded_file or not os.path.exists(downloaded_file):
        raise RuntimeError("Download did not complete in time")

    import hashlib
    def _fmd5(p):
        h = hashlib.md5()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    new_hash = _fmd5(downloaded_file)
    clips_dir = os.path.dirname(dest)
    if os.path.exists(clips_dir):
        import glob
        for existing in glob.glob(os.path.join(clips_dir, "clip_*.mp4")):
            if os.path.abspath(existing) != os.path.abspath(dest):
                if _fmd5(existing) == new_hash:
                    os.remove(downloaded_file)
                    raise RuntimeError(f"Duplicate download detected! Downloaded clip matches existing {os.path.basename(existing)} (MD5: {new_hash})")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(downloaded_file, dest)
    print(f"    saved {dest} ({os.path.getsize(dest)} bytes)")
    return dest

def run_pipeline(project: str = "javni-ki-dhun"):
    ensure_in_project()
    st = State()
    clips = st.project_clips(project)
    
    for job in clips:
        idx = job["idx"]
        dest = os.path.join(ROOT, "projects", project, "clips", f"clip_{idx:03d}.mp4")
        
        # Check if already downloaded and valid
        if os.path.exists(dest) and os.path.getsize(dest) > 100_000:
            print(f"Clip {idx} already exists on disk — marking done.")
            st.set_status(job["id"], "done", video_path=dest)
            continue
            
        print(f"\n[Flow Worker] Rendering {project} Clip {idx}...")
        initial_posters = set(get_posters())
        print(f"  existing posters before submit: {len(initial_posters)}")
        
        st.set_status(job["id"], "submitting", site="flow", account="a1")
        submit_prompt(job["prompt"])
        
        st.set_status(job["id"], "rendering", site="flow", account="a1")
        new_poster = poll_render(initial_posters, timeout=180.0)
        
        st.set_status(job["id"], "downloading", site="flow", account="a1")
        download_poster(new_poster, dest)
        
        st.set_status(job["id"], "done", video_path=dest)
        print(f"Clip {idx} successfully generated and recorded!")
        time.sleep(2)
        
    st.close()
    print("\nAll clips for project finished! Now triggering assembly & craft QC...")
    from runner import stitch_done_projects
    st2 = State()
    stitch_done_projects(st2)
    st2.close()

if __name__ == "__main__":
    proj = sys.argv[1] if len(sys.argv) > 1 else "dadaji-ki-yadein"
    run_pipeline(proj)
