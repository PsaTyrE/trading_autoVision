#!/usr/bin/env python3
"""Second opinion dari Muse sebelum entry. Butuh endpoint [OI]-compatible.

  Meta Model API : MUSE_BASE_URL=https://api.meta.ai/v1  MUSE_MODEL=muse-wonder-1.0  MUSE_API_KEY=...
  muse2api lokal : MUSE_BASE_URL=http://127.0.0.1:18610/v1  MUSE_MODEL=muse-chat  MUSE_API_KEY=m2a_...

Pakai:
  echo '<snap json>' | ./muse_consult.py
  ./muse_consult.py snap.json
  ./muse_consult.py --selftest

Output satu baris: VERDICT CONF | alasan.
Exit 0 = setuju entry (conf >= 0.85), 1 = tolak/ragu, 2 = error.
"""
import json
import os
import sys
import urllib.request

MIN_CONF = 0.85

SOP = (
    "Kamu analis risk crypto futures. Saldo kecil (~15 USDT), capital preservation mutlak. "
    "SOP wajib: entry hanya bila konfluensi volume tinggi (volume_ratio >= 0.90) + wick rejection "
    "di support/resistance, confidence >= 0.85, R:R >= 1.5. Jangan pernah memaksa entry; "
    "NO_TRADE adalah jawaban benar bila ragu atau data kurang. "
    "Jawab HANYA satu baris: VERDICT:<LONG|SHORT|NO_TRADE> CONF:<0.00-1.00> | <alasan, maks 25 kata>"
)


def ask(snap, timeout=120):
    base = os.environ.get("MUSE_BASE_URL", "http://127.0.0.1:18610/v1").rstrip("/")
    key = os.environ.get("MUSE_API_KEY", "")
    model = os.environ.get("MUSE_MODEL", "muse-chat")
    body = json.dumps({
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SOP},
            {"role": "user", "content": json.dumps(snap, ensure_ascii=False)},
        ],
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", body,
        {"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def parse(text):
    verdict, conf = "NO_TRADE", 0.0
    for tok in text.replace("|", " ").split():
        t = tok.rstrip(",.;")
        head, _, val = t.partition(":")
        if head.upper() == "VERDICT":
            verdict = val.upper()
        elif head.upper() == "CONF":
            try:
                conf = float(val)
            except ValueError:
                pass
    return verdict, conf


def render_prompt(snap):
    return SOP + "\n\nDATA:\n" + json.dumps(snap, indent=2, ensure_ascii=False)


def main():
    a = sys.argv[1:]
    if "--selftest" in a:
        assert parse("VERDICT:LONG CONF:0.91 | vol 1.2x wick ok") == ("LONG", 0.91)
        assert parse("verdict:no_trade conf:0.40 | data kurang") == ("NO_TRADE", 0.40)
        assert parse("ngawur") == ("NO_TRADE", 0.0)
        print("selftest ok")
        return
    if a and a[0] == "--prompt":
        src = open(a[1]).read() if len(a) > 1 else sys.stdin.read()
        print(render_prompt(json.loads(src)))
        return
    if a and a[0] == "--parse":
        text = a[1] if len(a) > 1 else sys.stdin.read()
        verdict, conf = parse(text)
        ok = verdict in ("LONG", "SHORT") and conf >= MIN_CONF
        print(f"{verdict} CONF:{conf:.2f} -> {'SETUJU' if ok else 'TOLAK'}")
        sys.exit(0 if ok else 1)
    src = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    snap = json.loads(src)
    try:
        text = ask(snap)
    except Exception as e:
        print(f"ERROR Muse: {type(e).__name__}: {e}")
        sys.exit(2)
    verdict, conf = parse(text)
    print(f"{verdict} CONF:{conf:.2f} | {text}")
    sys.exit(0 if verdict in ("LONG", "SHORT") and conf >= MIN_CONF else 1)


if __name__ == "__main__":
    main()
