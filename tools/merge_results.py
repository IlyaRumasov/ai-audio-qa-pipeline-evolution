#!/usr/bin/env python3
"""Офлайн-збірка звіту з результатів Vertex AI Batch (заміна ручних merge_*.py).

    python tools/merge_results.py --csv dataset.csv --results <папка з predictions*.jsonl> --out report.csv

На відміну від старих скриптів: шукає файли рекурсивно (і в папках з [дужками] у назві),
порожні та заблоковані відповіді позначає ERROR (а не OK), рядки без результату не губить,
дублікати не зливає за іменем файлу без шляху.
"""
import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import qa_core as q  # noqa: E402


def iter_jsonl(folder):
    for root, _, files in os.walk(folder):  # os.walk, не glob: glob ламається на '[CLIENT]' у шляху
        for f in sorted(files):
            if f.endswith(".jsonl"):
                yield os.path.join(root, f)


def uri_key(uri):
    """gs://bucket/.../audio/speaker_0/x.wav -> speaker_0/x.wav (спікер + ім'я)."""
    spk, base, _ = q.split_path(uri)
    return (spk + "/" if spk else "") + base


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    csv.field_size_limit(10 ** 9)
    with open(a.csv, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    # Файли, на які посилаються відповіді, = "диск" для зіставлення
    answers, dup, bad = {}, 0, 0
    tokens = {"prompt": 0, "output": 0, "thoughts": 0}
    for path in iter_jsonl(a.results):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    x = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                parts = (((x.get("request") or {}).get("contents") or [{}])[0] or {}).get("parts") or []
                uri = next((p["fileData"].get("fileUri", "") for p in parts if isinstance(p, dict) and p.get("fileData")), "")
                if not uri:
                    bad += 1
                    continue
                if uri in answers:
                    dup += 1
                    continue
                u = (x.get("response") or {}).get("usageMetadata") or {}
                tokens["prompt"] += u.get("promptTokenCount", 0)
                tokens["output"] += u.get("candidatesTokenCount", 0)
                tokens["thoughts"] += u.get("thoughtsTokenCount", 0)
                answers[uri] = x
    files = [{"id": uri, "path": uri_key(uri)} for uri in answers]
    results, orphans = q.match_rows(rows, files)
    report = []
    for r in results:
        row = rows[r["row"]]
        if not r["file_id"]:
            report.append(q.report_row(row, r["status"]))
            continue
        x = answers[r["file_id"]]
        report.append(q.report_row(row, r["status"], q.assess(row.get("transcript"), q.parse_response(x.get("response"), x.get("status")))))
    with open(a.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=q.REPORT_COLUMNS)
        w.writeheader()
        w.writerows(report)
    s = q.summarize(report)
    s.update({"answers_without_csv_row": len(orphans), "duplicate_answers": dup, "broken_lines": bad, "tokens": tokens})
    print(json.dumps(s, ensure_ascii=False, indent=1))
    return s


if __name__ == "__main__":
    main()
