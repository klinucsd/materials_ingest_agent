"""Ask a model the one question it is good at, and check every answer.

The semantic step, and the only place a model is used. Its job is narrow by
construction:

    for each property the target schema REQUIRES,
    does this source document provide it, and where exactly does it say so?

It is never asked what the schema requires -- requirements.py reads that from
the schema. It is never asked whether a value may be proposed -- taxonomy.py
decides that. It is never asked to write into the database -- it cannot reach
it. What is left is reading a document and saying what is in it, which is the
thing it does well and the thing nothing else here can do at all.

EVERY ANSWER IS CHECKED. A claim that the source states something must quote
the source, and the quote must appear in the source verbatim. A quote that does
not is a fabrication, and the claim is discarded and recorded as one rather
than carried forward. That check is cheap, mechanical, and catches the failure
this whole project is arranged against: a plausible value with nothing behind
it.

    map_source.py <source-file> --class ExposureEpisode [--model ...]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from requirements import requirements_for, summarise          # noqa: E402

PROVIDERS = {
    "nrp":    ("https://ellm.nrp-nautilus.io/v1", "NRP_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}


def load_env():
    """Read code/.env, then .env beside the repo. An exported value wins."""
    for path in (HERE / ".env", ROOT / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def resolve_model(spec):
    provider, _, rest = (spec or "").partition(":")
    if rest and provider in PROVIDERS:
        url, key_var = PROVIDERS[provider]
        return rest, url, key_var
    return spec, os.environ.get("MODEL_URL"), \
        os.environ.get("MODEL_API_KEY_ENV", "MODEL_API_KEY")


PROMPT = """\
You are reading one source document from a materials-science data delivery, to
work out which facts it does and does not state.

Below is what the target schema REQUIRES for one class. You did not choose
these requirements and you cannot change them; they are read from the schema.

{requirements}

Here is the source document, in full:

--- BEGIN SOURCE ---
{source}
--- END SOURCE ---

For EACH required property above, answer with one object:

  {{"property": "<name>",
    "stated": true | false,
    "quote": "<text copied EXACTLY from the source, or null>",
    "value": "<the value as the source gives it, or null>",
    "unit_as_written": "<the unit as the source writes it, or null>",
    "note": "<one sentence: how you read it, or what is missing>"}}

Rules you must follow:

* `quote` must be copied CHARACTER FOR CHARACTER from the source. It is checked
  against the source text and a quote that does not appear there is discarded.
  Never paraphrase into the quote, never tidy the spacing, never translate a
  unit. If you cannot quote it, `stated` is false.
* `stated: false` is a correct and useful answer. Most deliveries do not carry
  most of what a strict schema wants. Do NOT infer a value from convention,
  from another property, or from what is typical for this kind of experiment.
  If the document does not say it, it is not stated.
* Do not convert units or normalise values. Give them as written. Whether
  "eV" or "m^-2 s^-1" matches what the schema wants is decided elsewhere.
* Where a property is a link to another object rather than a value, `stated`
  means the source identifies that object at all.

Reply with a JSON array of those objects and nothing else.
"""


def ask(model, url, key, requirements_text, source_text, timeout=900,
        max_tokens=16000):
    import httpx
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": PROMPT.format(
            requirements=requirements_text, source=source_text)}],
    }
    r = httpx.post(f"{url}/chat/completions", json=body, timeout=timeout,
                   headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    payload = r.json()
    choice = payload["choices"][0]
    text = choice["message"].get("content") or ""
    usage = payload.get("usage", {})
    usage["finish_reason"] = choice.get("finish_reason")
    return text, usage


def extract_json(text):
    """The model may wrap the array in prose or a fence. Take the array."""
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _numbers(value):
    """Numeric tokens in a claimed value, normalised so 1.2e25 == 1.2E25."""
    out = []
    for tok in _NUM.findall(str(value or "")):
        try:
            out.append(repr(float(tok)))
        except ValueError:
            pass
    return out


def _source_numbers(text):
    return {repr(float(t)) for t in _NUM.findall(text)
            if _safe_float(t) is not None}


def _safe_float(t):
    try:
        return float(t)
    except ValueError:
        return None


def _normalise(s):
    """Whitespace-insensitive, so a reflowed quote still matches its source."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def verify(answers, source_text, required):
    """Check each answer against the source. Returns (checked, problems).

    Three things are checked, all mechanically:
      the property is one the schema actually requires;
      a claim that the source states something carries a quote;
      that quote appears in the source.
    """
    haystack = _normalise(source_text)
    names = {p["property"] for p in required}
    checked, problems = [], []
    seen = set()

    for a in answers if isinstance(answers, list) else []:
        prop = (a or {}).get("property")
        if prop not in names:
            problems.append({"kind": "unknown_property", "property": prop,
                             "detail": "not a property this class requires"})
            continue
        seen.add(prop)
        if not a.get("stated"):
            checked.append({**a, "verified": False, "stated": False})
            continue
        quote = a.get("quote")
        if not quote:
            problems.append({"kind": "claim_without_quote", "property": prop,
                             "detail": "said the source states it but quoted "
                                       "nothing"})
            continue
        if _normalise(quote) not in haystack:
            problems.append({"kind": "quote_not_in_source", "property": prop,
                             "detail": f"quoted {quote!r}, which does not "
                                       f"appear in the source"})
            continue

        # A quote that exists is necessary and not sufficient. The first run
        # returned value "480; 4260" quoting "Duration (s)" -- the column
        # HEADER, which appears in the source and substantiates nothing. So
        # every number in the claimed value must appear in the source too.
        # This does not prove the value was read from the right row; it does
        # stop a number arriving with no counterpart in the document at all.
        missing = [n for n in _numbers(a.get("value"))
                   if n not in _source_numbers(source_text)]
        if missing:
            problems.append({
                "kind": "value_not_in_source", "property": prop,
                "detail": f"value {a.get('value')!r} contains "
                          f"{', '.join(missing)}, which do not appear anywhere "
                          f"in the source"})
            continue
        weak = _normalise(str(a.get("value") or "")) not in _normalise(quote) \
            and _numbers(a.get("value"))
        checked.append({**a, "verified": True,
                        "quote_substantiates_value": not weak})

    for prop in sorted(names - seen):
        problems.append({"kind": "unanswered", "property": prop,
                         "detail": "the model did not answer for this property"})
    return checked, problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path)
    ap.add_argument("--class", dest="cls", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max-tokens", type=int, default=24000,
                    help="budget for ONE reply. Reasoning is charged "
                         "against it, and on this endpoint reasoning "
                         "can consume all of it")
    a = ap.parse_args(argv[1:])

    load_env()
    spec = a.model or os.environ.get("MODEL")
    if not spec:
        print("  no model set. Put MODEL=provider:name in code/.env")
        return 2
    model, url, key_var = resolve_model(spec)
    key = os.environ.get(key_var or "")
    if not url or not key:
        print(f"  cannot reach {spec!r}: need a URL and {key_var}")
        return 2

    if not a.source.is_file():
        print(f"  no such source: {a.source}")
        return 2
    source_text = a.source.read_text(errors="replace")

    req = requirements_for(a.cls)
    required = [p for p in req["properties"] if p["required"]]
    print(f"  source      {a.source.name}  ({len(source_text):,} chars)")
    print(f"  target      {a.cls}  -- {len(required)} required propert"
          f"{'y' if len(required) == 1 else 'ies'}")
    print(f"  model       {model}")
    print()

    budget = a.max_tokens
    for attempt in (1, 2):
        text, usage = ask(model, url, key, summarise(req), source_text,
                          max_tokens=budget)
        answers = extract_json(text)
        if answers is not None:
            break
        # An empty reply with finish_reason 'length' is not a refusal and not
        # a parse problem: the model spent its whole budget on reasoning and
        # had nothing left to say. Saying "did not return a JSON array" sends
        # the reader after the prompt, which is the wrong place entirely.
        reasoning = (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens", 0)
        if usage.get("finish_reason") == "length" and not text.strip():
            print(f"  the reply was cut off after {usage.get('completion_tokens', 0):,}"
                  f" tokens ({reasoning:,} of them reasoning) with nothing to"
                  f" act on.")
            if attempt == 1:
                budget *= 2
                print(f"  retrying with {budget:,}")
                continue
            print("  Raise --max-tokens, or give it less to do in one call.")
            return 1
        print("  the model did not return a JSON array"
              f"  (finish_reason={usage.get('finish_reason')},"
              f" {usage.get('completion_tokens', 0):,} tokens,"
              f" {reasoning:,} reasoning):")
        print("   ", (text[:400] or "<empty>").replace("\n", "\n    "))
        return 1

    checked, problems = verify(answers, source_text, required)
    stated = [c for c in checked if c.get("verified")]
    absent = [c for c in checked if not c.get("stated")]

    print(f"  STATED IN THE SOURCE ({len(stated)}):")
    for c in stated:
        v = c.get("value")
        u = c.get("unit_as_written")
        print(f"    {c['property']:<34} {str(v)[:22]:<24}"
              f"{('[' + str(u) + ']') if u else ''}")
        print(f"      quoted: {str(c.get('quote'))[:88]}")
        if c.get("quote_substantiates_value") is False:
            print(f"      NOTE: the quote does not contain the value -- it "
                  f"locates the field, not the number")
    print(f"\n  NOT STATED ({len(absent)}):")
    for c in absent:
        print(f"    {c['property']:<34} {str(c.get('note'))[:72]}")
    if problems:
        print(f"\n  REJECTED ({len(problems)}) -- claims that did not check out:")
        for p in problems:
            print(f"    {p['kind']:<22} {p.get('property')}: {p['detail'][:68]}")

    out = {
        "source": str(a.source), "class": a.cls, "model": model,
        "required": [p["property"] for p in required],
        "stated": stated, "absent": absent, "rejected": problems,
        "usage": usage,
    }
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(out, indent=1) + "\n")
        print(f"\n  wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
