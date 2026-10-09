"""Generate test cases (JSON) from a plain-English requirements document.

Usage: python generate_tests.py requirements.md --url https://app.example.com [--out tests/generated.json] [--max-cases 8]
"""
import argparse, json, os, re, sys
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent

PROMPT = """You are a QA engineer. From the requirements below, write end-to-end UI test cases for a website at {url}.
Each case is executed by an AI agent that sees the screen and can click, type, scroll and select dropdown options, so write the goal as concrete user actions (include exact credentials/data given in the requirements) and the expected result as something visible on screen.
Cover happy paths, validation/error paths, and at least one visual/UI-quality check where relevant. Max {n} cases. Do not invent features, credentials or data that are not in the requirements.
Reply with ONLY a JSON array of objects: {{"name": "...", "url": "{url}", "goal": "...", "expected": "..."}}.

REQUIREMENTS:
{req}"""


def main():
    agent.load_env()
    agent.MAX_TOKENS = 8000
    THINK = False  # reasoning only makes generation slower, not better
    ap = argparse.ArgumentParser()
    ap.add_argument("requirements")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out")
    ap.add_argument("--max-cases", type=int, default=8)
    a = ap.parse_args()
    req = open(a.requirements, encoding="utf8").read()
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"], timeout=240, max_retries=1)
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    reply = agent.ask(client, [{"role": "user", "content": PROMPT.format(url=a.url, n=a.max_cases, req=req)}], usage, think=False)
    m = re.search(r"\[.*\]", reply, re.S)
    if not m:
        sys.exit(f"model did not return a JSON array:\n{reply[:500]}")
    cases = json.loads(m.group(0))
    clean = []
    for c in cases:
        if all(k in c for k in ("name", "goal", "expected")):
            clean.append({"name": c["name"], "url": c.get("url") or a.url, "goal": c["goal"], "expected": c["expected"]})
    out = a.out or os.path.join(agent.HERE, "tests", "generated_" + os.path.splitext(os.path.basename(a.requirements))[0] + ".json")
    json.dump(clean, open(out, "w", encoding="utf8"), indent=2, ensure_ascii=False)
    print(f"wrote {len(clean)} cases -> {out}  ({usage['prompt'] + usage['completion']:,} tokens)")
    for c in clean:
        print(f"  - {c['name']}")


if __name__ == "__main__":
    main()
