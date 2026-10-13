"""Тест офлайн-збірки звіту на синтетичних результатах Vertex (папка з '[CLIENT]' у назві)."""
import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import merge_results  # noqa: E402


def answer(uri, text=None, finish="STOP", status=None, block=None):
    resp = {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3, "thoughtsTokenCount": 0}}
    if block:
        resp["promptFeedback"] = {"blockReason": block}
    else:
        resp["candidates"] = [{"finishReason": finish, "content": {"parts": [{"text": text}] if text is not None else []}}]
    line = {"request": {"contents": [{"role": "user", "parts": [{"text": "p"}, {"fileData": {"mimeType": "audio/wav", "fileUri": uri}}]}]},
            "response": resp}
    if status:
        line = {"request": line["request"], "status": status}
    return json.dumps(line)


class TestMerge(unittest.TestCase):
    def test_merge(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rows = [
                ("audio/speaker_0/AAA___00h00m00.000---00h00m02.000.wav", "ini adalah teks"),  # OK
                ("audio/speaker_0/AAA___00h00m02.000---00h00m04.000.wav", "satu dua tiga"),    # BRAK
                ("audio/speaker_1/BBB___00h00m00.000---00h00m02.000.wav", "halo"),             # порожня відповідь -> ERROR
                ("audio/speaker_1/BBB___00h00m02.000---00h00m04.000.wav", "x"),                # заблоковано -> ERROR
                ("audio/speaker_2/CCC___00h00m00.000---00h00m02.000.wav", "y"),                # результату немає -> MISSING
                ("audio/speaker_3/longid1234___00h00m09.000---00h00m10.000.wav", "z"),         # інший id (Bahasa) -> SPEAKER_TIMECODE
            ]
            with open(d / "dataset.csv", "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["file", "language", "speaker", "transcript", "corrected_transcript"])
                for f, t in rows:
                    w.writerow([f, "ind", f.split("/")[1], t, ""])
            res = d / "[CLIENT] out" / "prediction-model-1"
            res.mkdir(parents=True)
            g = "gs://b/Bahasa/audio/"
            lines = [
                answer(g + "speaker_0/AAA___00h00m00.000---00h00m02.000.wav", '{"verdict":"OK","transcription":"ini adalah teks","issues":[]}'),
                answer(g + "speaker_0/AAA___00h00m02.000---00h00m04.000.wav", '{"verdict":"BRAK","transcription":"satu tiga","issues":["dua missing"]}'),
                answer(g + "speaker_0/AAA___00h00m02.000---00h00m04.000.wav", '{"verdict":"OK","transcription":"dup","issues":[]}'),
                answer(g + "speaker_1/BBB___00h00m00.000---00h00m02.000.wav", None),
                answer(g + "speaker_1/BBB___00h00m02.000---00h00m04.000.wav", block="PROHIBITED_CONTENT"),
                answer(g + "speaker_3/a1b2c3d4___00h00m09.000---00h00m10.000.wav", '{"verdict":"MINOR","transcription":"z","issues":[]}'),
                "{broken json",
            ]
            (res / "predictions.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                s = merge_results.main(["--csv", str(d / "dataset.csv"), "--results", str(d / "[CLIENT] out"), "--out", str(d / "report.csv")])
            with open(d / "report.csv", encoding="utf-8-sig") as fh:
                out = list(csv.DictReader(fh))
            self.assertEqual([r["verdict"] for r in out], ["OK", "BRAK", "ERROR", "ERROR", "SKIPPED", "MINOR"])
            self.assertEqual([r["match"] for r in out], ["EXACT", "EXACT", "EXACT", "EXACT", "MISSING", "SPEAKER_TIMECODE"])
            self.assertEqual(out[1]["wer"], "0.333")
            self.assertEqual(s["duplicate_answers"], 1)
            self.assertEqual(s["broken_lines"], 1)
            self.assertEqual(s["tokens"]["prompt"], 50)


if __name__ == "__main__":
    unittest.main()
