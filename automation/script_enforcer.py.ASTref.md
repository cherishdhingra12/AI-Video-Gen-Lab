# AST map (auto-generated) — do not hand-edit
# This file keeps the current symbol table of automation/script_enforcer.py
# so a fix that removes dead names does not accidentally drop a live one.

# functions (19 unique, top-level or parsed-into)
FUNCTIONS = {
    "_sections",
    "_spoken_lines",
    "_word_count",
    "_is_preface_or_contents",
    "_beats_swappable",
    "_moral_earned",
    "_platitude_allowed",
    "_is_platitude",
    "_colloquial_question",
    "_moral_grounding",
    "_named_characters_present",
    "_concrete_present",
    "_dialogue_fit",
    "_budget_fit",
    "_length_ok",
    "_sameness_danger",
    "check",
    "enforce_before_credit",
    "ScriptHeld",
}

# Note: by-construction duplicate defs exist in the file for
#   _platitude_allowed  — two copies (both live in the current parse tree)
#   _is_platitude        — two copies (only the second is live)
#   _platitude_marks     — defined three times (the first two are dead)
#   _platitude_exempt    — defined three times (the first two are dead)
#
# Confirmed live surface after removing the two orphaned bodies (lines ~442-569)
# is the same as above. The orphaned blocks were:
#   - the first huge platitude_marks tuple starting at line ~206 in _moral_earned
#   - the orphaned tuple that begins immediately after _platitude_allowed (line ~442),
#     followed by the second dead _platitude_allowed (line ~442) and the second dead
#     _is_platitude (line ~590) and the third dead _platitude_marks (line ~596).
PY
