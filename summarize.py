"""Print one compact results table per model family, from the analysis JSON files.

Read-only. Makes no model call and no network call.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent / "results"

# Condition label -> short name, in report order.
ORDER = [
    ("main:k0:docs-unavailable:canaryfilter0:helperfilter0:reminder0", "forumonly k0"),
    ("main:k1:docs-unavailable:canaryfilter0:helperfilter0:reminder0", "forumonly k1"),
    ("main:k3:docs-unavailable:canaryfilter0:helperfilter0:reminder0", "forumonly k3"),
    ("main:k8:docs-unavailable:canaryfilter0:helperfilter0:reminder0", "forumonly k8"),
    ("main:k24:docs-unavailable:canaryfilter0:helperfilter0:reminder0", "forumonly k24"),
    ("main:k0:docs-available:canaryfilter0:helperfilter0:reminder0", "saferoute k0"),
    ("main:k1:docs-available:canaryfilter0:helperfilter0:reminder0", "saferoute k1"),
    ("main:k3:docs-available:canaryfilter0:helperfilter0:reminder0", "saferoute k3"),
    ("main:k8:docs-available:canaryfilter0:helperfilter0:reminder0", "saferoute k8"),
    ("main:k24:docs-available:canaryfilter0:helperfilter0:reminder0", "saferoute k24"),
    ("main:k8:docs-unavailable:canaryfilter0:helperfilter0:reminder1", "reminder k8"),
    ("waterbed:k8:docs-unavailable:canaryfilter0:helperfilter0:reminder0", "waterbed none"),
    ("waterbed:k8:docs-unavailable:canaryfilter1:helperfilter0:reminder0", "waterbed canary blocked"),
    ("waterbed:k8:docs-unavailable:canaryfilter0:helperfilter1:reminder0", "waterbed helper blocked"),
]
OUTCOMES = ("token_leak", "helper_run", "spontaneous_disclosure", "direct_admission")


def cell(rates, label, outcome):
    entry = rates.get(outcome, {}).get(label)
    if not entry or entry.get("total", 0) == 0:
        return "    -"
    return f"{entry['successes']:>3}/{entry['total']:<3}"


for family in ("runs_deepseek", "runs_glm53", "runs_qwen35"):
    path = ROOT / f"analysis_{family}.json"
    if not path.exists():
        continue
    data = json.loads(path.read_text("utf-8"))
    rates = data["rates"]
    print(f"\n===== {family}  ({data['run_count']} runs) =====")
    print(f"{'condition':<26} {'leak':>8} {'helper':>8} {'spont':>8} {'asked':>8}")
    for label, short in ORDER:
        cells = " ".join(f"{cell(rates, label, o):>8}" for o in OUTCOMES)
        print(f"{short:<26} {cells}")
    for key, name in (
        ("empirical_token_threshold", "forum-only leak threshold"),
        ("empirical_token_threshold_saferoute", "safe-route leak threshold"),
    ):
        if key in data:
            print(f"{name}: {data[key]['threshold_k']}")
    sub = data.get("channel_substitution")
    if sub:
        print("channel substitution:", json.dumps(sub, sort_keys=True)[:400])

print("\nColumns: leak = canary posted, helper = untrusted helper run,")
print("spont = disclosed unprompted, asked = admitted when asked directly.")
