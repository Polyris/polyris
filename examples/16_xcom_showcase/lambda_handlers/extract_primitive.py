"""Producer #2: Lambda that returns a primitive — the regression case fixed in 0.100.0.

Deploy this as `polyris-xcom-extract-primitive` Lambda function.
Runtime: python3.12+. No polyris SDK required.

Pre-0.100.0 behaviour (the bug):
    The wrapper's Get_Dep_Output JSONata used a $isJson heuristic that only
    recognised plain objects and string/object arrays. Every other JSON type —
    primitives (42, null, true), arrays of numbers ([1,2,3]) — fell through
    to a {"_raw": raw_string} wrapper.

    Downstream code:
        value = event["upstream"]["extract_primitive"]["output"]   # → {"_raw": "42"}
        value + 1                                                  # TypeError

Post-0.100.0 (the fix):
    $exists($parse($safe)) uses JSONata's $parse to detect any valid JSON
    shape. Primitives flow through untouched.

    Downstream:
        value = xcom.get(event, "extract_primitive")   # → 42

Try each of the returns below (comment one, uncomment another) to see:
    - int      → renders as `42` in Task Detail Output tab
    - list     → renders as [1, 2, 3]
    - None     → renders as null
    - bool     → renders as true
    - str      → renders as "hello"

Optional: randomly raise to demonstrate the failed-upstream Console banner
and the report handler's raise_on_failure=False opt-in.
"""


def handler(_event, _context):
    # Pick one — all four are now correctly transported end-to-end:
    return 42
    # return [1, 2, 3]
    # return None
    # return True
    # return "hello"

    # To see the "failed dep" banner in the report task's Console Input tab,
    # uncomment this line and remove the return above:
    # raise RuntimeError("intentional failure to demo XComUpstreamFailedError")
