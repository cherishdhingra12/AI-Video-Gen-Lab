# Dead-block map — what the AST-driven cut removes

File: automation/script_enforcer.py
Size: 1128 lines, 57426ced7c84636a5cef2a3eea329ebd010add19363f9188522781354e53d716

BLOCK A — first orphaned tuple in _moral_earned
  Starts at line 206: the `platitude_marks = (` that opens the 74-line tuple
    "
    platitude_marks = (
        "kya yaadein hamesha saath rehti hain",
        "always remember",
        ...
        "always be",
    )
    "
  Ends at line 252 (the closing ")" of that tuple).
  This tuple is never referenced: the only live `platitude_marks` assignment is at
  line 390 (inside the surviving second body), and later the `if any(m in low ...
  ... platitude_marks)` arm uses that live binding.

BLOCK B — orphaned tuple + dead defs + duplicate tuple
  Starts at line 442:
    "
        "dream big",
        "follow your",
        ...
    "
  This is a bare tuple literal (leftover from a bad paste) sitting between
  `_platitude_allowed` and the next real def. Ends at line 570 (the ")" and the
  trailing blank/gap before the live code resumes).
  Includes:
    - the orphaned tuple literal (lines 442-570)
    - the dead second `_platitude_allowed` starting at line 442 is actually just
      the name line; the literal that follows is the orphaned tuple.
  This block is entirely dead: there is no assignment, no def, no use; it is a
  top-level expression statement that the AST treats as a bare tuple literal.

BLOCK C — dead second _is_platitude + its platitude_marks
  Starts at line 590: `def _is_platitude(moral: str) -> bool:` (the second copy).
  Ends at line 648 (the closing ")" of its platitude_marks tuple and the function
  return path that does NOT include a return).
  This whole second def is dead: the live `_is_platitude` is the one at line 590
  ... actually the LIVE one is the second one. We must verify with AST before cutting.
  (The inventory above already flagged this ambiguity.)

BLOCK D — third dead platitude_marks (inside dead _is_platitude)
  This is BLOCK C's internal tuple; cut together with C if C is dead.

DECISION (confirmed by AST):
  - Block A: dead — cut.
  - Block B: dead — cut.
  - Block C: alive — KEEP (only live `_is_platitude`).
  - BLOCK D: part of C — keep with C.

So the surgical cut is:
  1) lines 206 through 252 inclusive  (end each with newline preserved)
  2) lines 442 through 570 inclusive
  Do NOT touch lines 590-648 (the live _is_platitude).
