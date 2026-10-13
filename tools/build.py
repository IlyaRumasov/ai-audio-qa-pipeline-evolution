#!/usr/bin/env python3
"""Збирає два флоу n8n із src/qa_core.js:

  workflows/audio-qa-sync.json          режим A: Drive + Gemini API + Google Sheets (вибірки, пілоти)
  workflows/audio-qa-batch-vertex.json  режим B: GCS + Vertex AI Batch Prediction (весь датасет)

У репозиторії замість ID і кредів стоять заглушки <...>. Для розгортання:
    python tools/build.py --private context/deploy.json --out context/build
(deploy.json не комітиться: ID таблиць, папок, бакета, кредів).
"""
import argparse
import copy
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = (ROOT / "src" / "qa_core.js").read_text(encoding="utf-8")
NS = uuid.UUID("6b1d3c1e-6f0e-4a8e-9f7a-2b0d3a4c5e6f")


def nid(wf, name):
    return str(uuid.uuid5(NS, wf + "/" + name))


class WF:
    def __init__(self, key, name):
        self.key, self.name, self.nodes, self.conn = key, name, [], {}

    def add(self, name, type_, ver, params, pos, **extra):
        n = {"id": nid(self.key, name), "name": name, "type": type_, "typeVersion": ver,
             "position": list(pos), "parameters": params}
        n.update(extra)
        self.nodes.append(n)
        return name

    def link(self, a, b, out=0, inp=0):
        lst = self.conn.setdefault(a, {}).setdefault("main", [])
        while len(lst) <= out:
            lst.append([])
        lst[out].append({"node": b, "type": "main", "index": inp})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    def json(self):
        return {"name": self.name, "nodes": self.nodes, "connections": self.conn,
                "settings": {"executionOrder": "v1", "errorWorkflow": "<ERROR_WORKFLOW_ID>",
                             "saveManualExecutions": True}}


def code(js, with_core=True):
    return {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": (CORE + "\n" if with_core else "") + js.strip() + "\n"}


def setcfg(fields):
    a = []
    for k, (v, t) in fields.items():
        a.append({"id": str(uuid.uuid5(NS, "cfg/" + k)), "name": k, "value": v, "type": t})
    return {"mode": "manual", "assignments": {"assignments": a}, "options": {}}


def rl_id(expr):
    return {"__rl": True, "mode": "id", "value": expr}


def if_eq(left, right, op="equals", typ="string"):
    operator = {"type": typ, "operation": op}
    if op in ("true", "false"):
        operator["singleValue"] = True
    return {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                           "conditions": [{"id": str(uuid.uuid5(NS, left + str(right))), "leftValue": left, "rightValue": right,
                                           "operator": operator}],
                           "combinator": "and"}, "options": {}}


def sticky(wf, name, text, pos, w=520, h=300):
    wf.add(name, "n8n-nodes-base.stickyNote", 1, {"content": text, "width": w, "height": h}, pos)


CRED_DRIVE = {"googleDriveOAuth2Api": {"id": "<DRIVE_CRED_ID>", "name": "Google Drive (dataset)"}}
CRED_SHEETS = {"googleSheetsOAuth2Api": {"id": "<SHEETS_CRED_ID>", "name": "Google Sheets (results)"}}
CRED_GEMINI = {"googlePalmApi": {"id": "<GEMINI_CRED_ID>", "name": "Gemini API key"}}
CRED_GCP = {"googleApi": {"id": "<GCP_SA_CRED_ID>", "name": "Google Service Account (GCS + Vertex AI)"}}
CRED_GMAIL = {"gmailOAuth2": {"id": "<GMAIL_CRED_ID>", "name": "Gmail (notifications)"}}

GEMINI_HTTP_AUTH = {"authentication": "predefinedCredentialType", "nodeCredentialType": "googlePalmApi"}
GCP_HTTP_AUTH = {"authentication": "predefinedCredentialType", "nodeCredentialType": "googleApi"}

# ------------------------------------------------------------------ режим A

PLAN_JS = r"""
const cfg = $('Налаштування').first().json;
const rows = $('CSV у рядки').all().map(i => i.json);
if (!rows.length) throw new Error('CSV порожній: перевірте csv_file_id у вузлі «Налаштування».');
if (!('file' in rows[0]) || !('transcript' in rows[0])) {
  throw new Error('У CSV немає колонок file і transcript. Знайдено: ' + Object.keys(rows[0]).join(', '));
}
// Папки спікерів: id -> назва (корінь має порожню назву)
const folders = $('Папки спікерів').all();
const folderName = new Map(folders.map(i => [i.json.id, i.json.name || '']));
const seen = new Set();
const files = [];
for (const it of $('Drive: файли в папках').all()) {
  const f = it.json;
  if (!f.id || seen.has(f.id)) continue;
  if (!/\.(wav|mp3|flac|ogg|m4a)$/i.test(f.name || '')) continue;
  seen.add(f.id);
  // Папка спікера: за зв'язком елементів n8n (pairedItem), запасний варіант: поле parents
  const pi = Array.isArray(it.pairedItem) ? it.pairedItem[0] : it.pairedItem;
  let parent = pi && folders[pi.item] ? (folders[pi.item].json.name || '') : undefined;
  if (parent === undefined) parent = (f.parents || []).map(p => folderName.get(p)).find(x => x !== undefined) || '';
  files.push({ id: f.id, path: (parent ? parent + '/' : '') + f.name, mimeType: f.mimeType || '' });
}
const mime = new Map(files.map(f => [f.id, f.mimeType]));
// Відновлення: рядки з готовим вердиктом не перевіряємо повторно (ERROR перевіряємо знову)
const done = new Set();
for (const it of $('Sheets: вже перевірені').all()) {
  const r = it.json || {};
  if (r.error) throw new Error('Не вдалося прочитати таблицю результатів: ' + (r.error.message || r.error) + '. Перевірте results_sheet_id і results_sheet_tab.');
  if (r.file && (['OK', 'MINOR', 'BRAK'].includes(r.verdict) || ['MISSING', 'AMBIGUOUS'].includes(r.match))) done.add(String(r.file));
}
const { results, orphans } = matchRows(rows, files);
const limit = Number(cfg.max_items) || Infinity;
const step = Math.max(1, Math.floor(Number(cfg.sample_step) || 1));  // 1 = усі рядки, N = кожен N-й
const out = [];
let todo = 0, skippedDone = 0;
for (const r of results) {
  if (r.row % step !== 0) continue;
  const row = rows[r.row];
  if (done.has(String(row.file))) { skippedDone++; continue; }
  if (!r.file_id) {
    out.push({ json: Object.assign({ _kind: 'nomatch' }, reportRow(row, r.status), { model: '', tokens: '', checked_at: $now.toISO() }) });
  } else if (todo < limit) {
    todo++;
    out.push({ json: { _kind: 'check', _file_id: r.file_id, _mime: mime.get(r.file_id) || 'audio/wav', _match: r.status,
      file: row.file, speaker: row.speaker || '', transcript: row.transcript } });
  }
}
const plan = { csv_rows: rows.length, sample_step: step, audio_files: files.length, orphans: orphans.length, already_done: skippedDone,
  to_check: todo, without_audio: out.filter(i => i.json._kind === 'nomatch').length };
if (!out.length) return [{ json: { _kind: 'nothing', plan } }];
out[0].json._plan = plan;
return out;
"""

FOLDERS_JS = r"""
const cfg = $('Налаштування').first().json;
const out = [{ json: { id: cfg.audio_folder_id, name: '' } }];
for (const it of $input.all()) {
  if (it.json.mimeType === 'application/vnd.google-apps.folder') out.push({ json: { id: it.json.id, name: it.json.name } });
}
return out;
"""

REQUEST_JS = r"""
// Крок 1: сліпа транскрипція. Еталон моделі не передаємо.
const cfg = $('Налаштування').first().json;
const src = $('Цикл по батчах').all();
const out = [];
for (let i = 0; i < $input.all().length; i++) {
  const row = src[i].json;
  const buf = await this.helpers.getBinaryDataBuffer(i, 'data');
  if (buf.length > 19 * 1024 * 1024) throw new Error('Файл більший за 19 MB, для inline-запиту завеликий: ' + row.file);
  const body = {
    contents: [{ role: 'user', parts: [
      { inlineData: { mimeType: row._mime || 'audio/wav', data: buf.toString('base64') } },
      { text: buildTranscribePrompt(cfg.language, cfg.script) },
    ] }],
    generationConfig: generationConfig(cfg.thinking_budget, TRANSCRIBE_SCHEMA),
  };
  out.push({ json: { row, body } });
}
return out;
"""

JUDGE_REQ_JS = r"""
// Розбір кроку 1 і правила. Запит порівняння формуємо для кожного рядка (лінійний флоу),
// але для рядків, які вирішили правила, його результат ігнорується.
const cfg = $('Налаштування').first().json;
const reqs = $('Запит: транскрипція').all();
return $input.all().map((it, i) => {
  const row = reqs[i].json.row;
  const r = it.json || {};
  const tr = r.error
    ? { ok: false, has_speech: false, transcription: '', error: 'HTTP: ' + String(r.error.message || JSON.stringify(r.error)).slice(0, 300) }
    : parseTranscription(r);
  const decided = decide(row.transcript, tr);
  const u = r.usageMetadata || {};
  const body = {
    contents: [{ role: 'user', parts: [{ text: buildJudgePrompt(row.transcript, tr.transcription, cfg.language) }] }],
    generationConfig: generationConfig(cfg.thinking_budget, JUDGE_SCHEMA),
  };
  return { json: { row, tr, decided, tokens: u.totalTokenCount || 0, model: r.modelVersion || cfg.model, body } };
});
"""

PARSE_JS = r"""
const prev = $('Запит: порівняння').all();
return $input.all().map((it, i) => {
  const p = prev[i].json;
  const r = it.json || {};
  let final = p.decided;
  let tokens = p.tokens;
  if (!final) {
    const judge = r.error
      ? { verdict: 'ERROR', transcription: '', issues: [], error: 'HTTP: ' + String(r.error.message || JSON.stringify(r.error)).slice(0, 300) }
      : parseResponse(r);
    final = combine(p.tr, judge);
    tokens += ((r.usageMetadata || {}).totalTokenCount || 0);
  }
  const rep = reportRow(p.row, p.row._match, assess(p.row.transcript, final));
  return { json: Object.assign(rep, { model: p.model, tokens, checked_at: $now.toISO() }) };
});
"""

SUMMARY_JS = r"""
const all = [];
for (let run = 0; ; run++) {
  let items;
  try { items = $('Розбір відповіді').all(0, run); } catch (e) { break; }
  if (!items || !items.length) break;
  for (const it of items) all.push(it.json);
}
let plan = {};
try { plan = ($('Зіставлення і план').first().json._plan) || $('Зіставлення і план').first().json.plan || {}; } catch (e) {}
const nomatch = $('Зіставлення і план').all().filter(i => i.json._kind === 'nomatch').map(i => i.json);
return [{ json: { plan, checked: summarize(all), without_audio: summarize(nomatch) } }];
"""


def build_sync():
    w = WF("sync", "[Audio AI-QA] A. Швидка перевірка (Drive + Gemini + Sheets)")
    x, y = 0, 300
    sticky(w, "Опис флоу", (
        "## Audio Dataset AI-QA, режим A: швидка перевірка\n"
        "Для вибірок і пілотів (сотні або тисячі файлів). Весь датасет: режим B (Vertex AI Batch).\n\n"
        "1. **Налаштування**: ID CSV і папки з аудіо на Drive, таблиця результатів (назва вкладки внизу таблиці),\n"
        "   мова, письмо, модель, ліміт max_items і вибірка sample_step (кожен N-й рядок CSV).\n"
        "2. Зіставлення рядків CSV з файлами: точний шлях, інакше спікер + таймкод, інакше MISSING/AMBIGUOUS (без вгадування).\n"
        "3. Крок 1: Gemini слухає аудіо БЕЗ еталона (сліпа транскрипція; тиша = has_speech false).\n"
        "   Крок 2: текстове порівняння еталону з транскрипцією: verdict OK / MINOR / BRAK і список помилок.\n"
        "   Тишу й точний збіг вирішують правила, результат другого запиту для них не використовується.\n"
        "4. Код рахує WER/CER і ставить прапорець CHECK, коли вердикт суперечить метриці.\n"
        "5. Результат пишеться в Sheets (upsert за колонкою file). Повторний запуск продовжує з місця зупинки,\n"
        "   рядки з ERROR перевіряються знову.\n\n"
        "Креди: Google Drive OAuth, Google Sheets OAuth, Gemini API key."), (x - 40, y - 380), 760, 340)
    t = w.add("Ручний запуск", "n8n-nodes-base.manualTrigger", 1, {}, (x, y))
    c = w.add("Налаштування", "n8n-nodes-base.set", 3.4, setcfg({
        "csv_file_id": ("<CSV_FILE_ID>", "string"),
        "audio_folder_id": ("<AUDIO_FOLDER_ID>", "string"),
        "results_sheet_id": ("<RESULTS_SHEET_ID>", "string"),
        "results_sheet_tab": ("Results", "string"),
        "language": ("Indonesian", "string"),
        "script": ("Latin", "string"),
        "model": ("gemini-2.5-flash", "string"),
        "thinking_budget": ("0", "string"),
        "max_items": (200, "number"),
        "sample_step": (1, "number"),
        "batch_size": (10, "number"),
    }), (x + 220, y))
    dl = w.add("Drive: завантажити CSV", "n8n-nodes-base.googleDrive", 3,
               {"operation": "download", "fileId": rl_id("={{ $json.csv_file_id }}"), "options": {}},
               (x + 440, y), credentials=CRED_DRIVE, executeOnce=True)
    ex = w.add("CSV у рядки", "n8n-nodes-base.extractFromFile", 1, {"options": {}}, (x + 660, y))
    ls = w.add("Drive: папка з аудіо", "n8n-nodes-base.googleDrive", 3, {
        "resource": "fileFolder", "returnAll": True,
        "filter": {"folderId": rl_id("={{ $('Налаштування').first().json.audio_folder_id }}"), "whatToSearch": "folders"},
        "options": {"fields": ["id", "name", "mimeType"]}}, (x + 880, y), credentials=CRED_DRIVE,
        executeOnce=True, alwaysOutputData=True)
    fo = w.add("Папки спікерів", "n8n-nodes-base.code", 2, code(FOLDERS_JS, False), (x + 1100, y))
    lf = w.add("Drive: файли в папках", "n8n-nodes-base.googleDrive", 3, {
        "resource": "fileFolder", "returnAll": True,
        "filter": {"folderId": rl_id("={{ $json.id }}"), "whatToSearch": "files"},
        "options": {"fields": ["id", "name", "mimeType"]}}, (x + 1320, y), credentials=CRED_DRIVE, alwaysOutputData=True)
    sheet_doc = rl_id("={{ $('Налаштування').first().json.results_sheet_id }}")
    sheet_tab = {"__rl": True, "mode": "name", "value": "={{ $('Налаштування').first().json.results_sheet_tab }}"}
    rd = w.add("Sheets: вже перевірені", "n8n-nodes-base.googleSheets", 4.7, {
        "operation": "read", "documentId": sheet_doc, "sheetName": sheet_tab, "options": {}},
        (x + 1540, y), credentials=CRED_SHEETS, executeOnce=True, alwaysOutputData=True, onError="continueRegularOutput")
    pl = w.add("Зіставлення і план", "n8n-nodes-base.code", 2, code(PLAN_JS), (x + 1760, y))
    iff = w.add("Потрібна перевірка ШІ?", "n8n-nodes-base.if", 2.2, if_eq("={{ $json._kind }}", "check"), (x + 1980, y))
    nm = w.add("Є рядки без аудіо?", "n8n-nodes-base.if", 2.2, if_eq("={{ $json._kind }}", "nomatch"), (x + 2200, y + 260))
    cols_all = ["file", "speaker", "match", "verdict", "flag", "wer", "cer", "reference", "model_transcript", "issues", "error", "model", "tokens", "checked_at"]
    schema = [{"id": k, "displayName": k, "required": False, "defaultMatch": False, "display": True,
               "type": "string", "canBeUsedToMatch": True} for k in cols_all]

    def upsert(name, pos):
        return w.add(name, "n8n-nodes-base.googleSheets", 4.7, {
            "operation": "appendOrUpdate", "documentId": sheet_doc, "sheetName": sheet_tab,
            "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": ["file"], "schema": schema},
            "options": {"handlingExtraData": "ignoreIt", "cellFormat": "RAW"}},
            pos, credentials=CRED_SHEETS, retryOnFail=True, maxTries=3, waitBetweenTries=5000)
    w_nm = upsert("Sheets: рядки без аудіо", (x + 2420, y + 260))
    lp = w.add("Цикл по батчах", "n8n-nodes-base.splitInBatches", 3,
               {"batchSize": "={{ $('Налаштування').first().json.batch_size }}", "options": {}}, (x + 2200, y - 20))
    da = w.add("Drive: завантажити аудіо", "n8n-nodes-base.googleDrive", 3,
               {"operation": "download", "fileId": rl_id("={{ $json._file_id }}"), "options": {}},
               (x + 2420, y + 60), credentials=CRED_DRIVE, retryOnFail=True, maxTries=3, waitBetweenTries=3000)
    rq = w.add("Запит: транскрипція", "n8n-nodes-base.code", 2, code(REQUEST_JS), (x + 2640, y + 60))

    def gemini(name, pos):
        return w.add(name, "n8n-nodes-base.httpRequest", 4.2, dict(GEMINI_HTTP_AUTH, **{
            "method": "POST",
            "url": "=https://generativelanguage.googleapis.com/v1beta/models/{{ $('Налаштування').first().json.model }}:generateContent",
            "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.body) }}",
            "options": {"timeout": 120000, "batching": {"batch": {"batchSize": 5, "batchInterval": 1000}}}}),
            pos, credentials=CRED_GEMINI, retryOnFail=True, maxTries=3, waitBetweenTries=10000,
            onError="continueRegularOutput")
    gm = gemini("Gemini: транскрипція", (x + 2860, y + 60))
    jq = w.add("Запит: порівняння", "n8n-nodes-base.code", 2, code(JUDGE_REQ_JS), (x + 3080, y + 60))
    gj = gemini("Gemini: порівняння", (x + 3300, y + 60))
    pr = w.add("Розбір відповіді", "n8n-nodes-base.code", 2, code(PARSE_JS), (x + 3520, y + 60))
    w_ok = upsert("Sheets: записати результат", (x + 3740, y + 60))
    sm = w.add("Підсумок", "n8n-nodes-base.code", 2, code(SUMMARY_JS), (x + 2420, y - 200))
    w.chain(t, c, dl, ex, ls, fo, lf, rd, pl, iff)
    w.link(iff, lp, 0)
    w.link(iff, nm, 1)
    w.link(nm, w_nm, 0)
    w.link(lp, sm, 0)
    w.link(lp, da, 1)
    w.chain(da, rq, gm, jq, gj, pr, w_ok)
    w.link(w_ok, lp)
    return apply_layout(w, (
        "# Audio AI-QA · A. Швидка перевірка (вибірки й пілоти)\n"
        "Drive + Gemini API + Google Sheets. Крок 1: модель слухає аудіо без еталона. Крок 2: текстове порівняння.\n"
        "Повторний запуск продовжує з місця зупинки (upsert за колонкою file). Весь датасет: режим B."),
        [(0, 7, "1. Підготовка", "CSV і папки спікерів з Drive → зіставлення → що вже перевірено", [t, c, dl, ex, ls, fo, lf, rd, pl]),
         (1, 6, "2. Розподіл", "є аудіо → перевірка; немає → одразу в таблицю", [iff, nm, w_nm]),
         (1, 5, "4. Підсумок", "лічильники запуску", [sm]),
         (2, 4, "3. Перевірка батчами", "сліпа транскрипція → правила → порівняння → таблиця", [lp, da, rq, gm, jq, gj, pr, w_ok])])


# ------------------------------------------------------------------ режим B

MATCH_B_JS = r"""
const cfg = $('Налаштування').first().json;
const rows = $('CSV у рядки').all().map(i => i.json);
if (!rows.length) throw new Error('CSV порожній: перевірте csv_object.');
if (!('file' in rows[0]) || !('transcript' in rows[0])) {
  throw new Error('У CSV немає колонок file і transcript. Знайдено: ' + Object.keys(rows[0]).join(', '));
}
const prefix = String(cfg.audio_prefix || '').replace(/^\/+/, '');
const MIME = { wav: 'audio/wav', mp3: 'audio/mpeg', flac: 'audio/flac', ogg: 'audio/ogg', m4a: 'audio/mp4' };
const files = [];
for (const it of $('GCS: список аудіо').all()) {
  for (const o of (it.json.items || [])) {
    const ext = (o.name.match(/\.([a-z0-9]+)$/i) || [])[1];
    if (!ext || !MIME[ext.toLowerCase()]) continue;
    files.push({ id: 'gs://' + cfg.bucket + '/' + o.name, path: o.name.slice(prefix.length), mime: MIME[ext.toLowerCase()] });
  }
}
const mime = new Map(files.map(f => [f.id, f.mime]));
const { results, orphans } = matchRows(rows, files);
const lines = [];
const meta = [];
const st = {};
for (const r of results) {
  const row = rows[r.row];
  st[r.status] = (st[r.status] || 0) + 1;
  meta.push({ file: row.file, speaker: row.speaker || '', transcript: row.transcript, match: r.status, uri: r.file_id || '' });
  // Крок 1: сліпа транскрипція, еталону в запиті немає
  if (r.file_id) lines.push(JSON.stringify(buildTranscribeLine(r.file_id, cfg.language, cfg.script, cfg.thinking_budget, mime.get(r.file_id))));
}
if (!lines.length) throw new Error('Жодного аудіо не зіставлено з CSV (' + files.length + ' файлів у gs://' + cfg.bucket + '/' + prefix + '). Перевірте audio_prefix.');
const run = cfg.output_prefix.replace(/\/+$/, '') + '/' + cfg.run_id;
const jsonl = await this.helpers.prepareBinaryData(Buffer.from(lines.join('\n') + '\n', 'utf8'), 'batch1.jsonl', 'application/jsonl');
const metaBin = await this.helpers.prepareBinaryData(Buffer.from(JSON.stringify(meta), 'utf8'), 'rows.json', 'application/json');
// Два файли для бакета одним вузлом завантаження: службовий rows.json і пакет кроку 1
const info = { run, csv_rows: rows.length, audio_files: files.length, requests: lines.length, orphans: orphans.length, match: st };
return [
  { json: Object.assign({ object: run + '/rows.json' }, info), binary: { data: metaBin } },
  { json: Object.assign({ object: run + '/batch1.jsonl' }, info), binary: { data: jsonl } },
];
"""

STAGE2_JS = r"""
// Розбір кроку 1 (транскрипції), правила і пакет для кроку 2 (порівняння) лише для невирішених рядків.
const cfg = $('Налаштування').first().json;
const meta = JSON.parse($('GCS: завантажити rows.json').first().json.data);
const byUri = new Map();
meta.forEach((m, i) => { if (m.uri) byUri.set(m.uri, i); });
const tr = new Array(meta.length).fill(null);
let dup = 0, unknown = 0, bad = 0;
const tokens = { prompt: 0, output: 0, thoughts: 0 };
for (const it of $('Крок 1: задача Vertex').all()) {
  for (const line of String(it.json.data || '').split('\n')) {
    if (!line.trim()) continue;
    let x;
    try { x = JSON.parse(line); } catch (e) { bad++; continue; }
    const parts = ((((x.request || {}).contents || [])[0]) || {}).parts || [];
    const fd = parts.find(p => p && p.fileData);
    const i = byUri.get(fd ? fd.fileData.fileUri : '');
    if (i === undefined) { unknown++; continue; }
    if (tr[i]) { dup++; continue; }
    const u = (x.response || {}).usageMetadata || {};
    tokens.prompt += u.promptTokenCount || 0;
    tokens.output += u.candidatesTokenCount || 0;
    tokens.thoughts += u.thoughtsTokenCount || 0;
    tr[i] = parseTranscription(x.response, x.status);
  }
}
const interim = meta.map((m, i) => {
  if (!m.uri) return Object.assign({}, m, { tr: null, decided: null });
  const t = tr[i] || { ok: false, has_speech: false, transcription: '', error: 'no result in batch output' };
  return Object.assign({}, m, { tr: t, decided: decide(m.transcript, t) });
});
const lines = [];
interim.forEach((m, i) => {
  if (m.uri && !m.decided) lines.push(JSON.stringify(buildJudgeLine(i, m.transcript, m.tr.transcription, cfg.language, cfg.thinking_budget)));
});
const run = $('Зіставлення і пакет').first().json.run;
const info = { run, judge_requests: lines.length, decided_by_rules: interim.filter(m => m.decided).length,
  stage1: { duplicate_results: dup, unknown_results: unknown, broken_lines: bad, tokens } };
const out = [{ json: Object.assign({ object: run + '/interim.json' }, info),
  binary: { data: await this.helpers.prepareBinaryData(Buffer.from(JSON.stringify(interim), 'utf8'), 'interim.json', 'application/json') } }];
if (lines.length) out.push({ json: Object.assign({ object: run + '/batch2.jsonl' }, info),
  binary: { data: await this.helpers.prepareBinaryData(Buffer.from(lines.join('\n') + '\n', 'utf8'), 'batch2.jsonl', 'application/jsonl') } });
return out;
"""

REPORT_JS = r"""
const cfg = $('Налаштування').first().json;
const interim = JSON.parse($('GCS: завантажити interim.json').first().json.data);
const s1 = $('Транскрипції і пакет 2').first().json.stage1;
let judgeItems = [];
try { judgeItems = $('Крок 2: задача Vertex').all(); } catch (e) { judgeItems = []; }  // крок 2 міг не знадобитися
const judge = new Array(interim.length).fill(null);
let dup = 0, unknown = 0, bad = 0;
const tokens = Object.assign({}, s1.tokens);
for (const it of judgeItems) {
  for (const line of String(it.json.data || '').split('\n')) {
    if (!line.trim()) continue;
    let x;
    try { x = JSON.parse(line); } catch (e) { bad++; continue; }
    const id = caseIdOf(x.request);
    if (id === null || !interim[id]) { unknown++; continue; }
    if (judge[id]) { dup++; continue; }
    const u = (x.response || {}).usageMetadata || {};
    tokens.prompt += u.promptTokenCount || 0;
    tokens.output += u.candidatesTokenCount || 0;
    tokens.thoughts += u.thoughtsTokenCount || 0;
    judge[id] = parseResponse(x.response, x.status);
  }
}
const missing = { verdict: 'ERROR', transcription: '', issues: [], error: 'no result in batch output' };
const report = interim.map((m, i) => {
  const row = { file: m.file, speaker: m.speaker, transcript: m.transcript };
  if (!m.uri) return reportRow(row, m.match);
  const final = m.decided || combine(m.tr, judge[i] || missing);
  return reportRow(row, m.match, assess(m.transcript, final));
});
const summary = summarize(report);
Object.assign(summary, {
  decided_by_rules: interim.filter(m => m.decided).length,
  judge_requests: $('Транскрипції і пакет 2').first().json.judge_requests,
  duplicate_results: s1.duplicate_results + dup, unknown_results: s1.unknown_results + unknown,
  broken_lines: s1.broken_lines + bad, tokens });
const run = $('Зіставлення і пакет').first().json.run;
const csv = await this.helpers.prepareBinaryData(Buffer.from(toCsv(report), 'utf8'), 'audio_qa_report_' + cfg.run_id + '.csv', 'text/csv');
return [{ json: { run, report_object: run + '/report.csv', summary }, binary: { csv } }];
"""

MAIL_OK_JS = r"""
const s = $('Звіт').first().json.summary;
const cfg = $('Налаштування').first().json;
const rows = Object.entries(s).filter(([k]) => k !== 'tokens')
  .map(([k, v]) => '<tr><td style="padding:2px 12px 2px 0">' + k + '</td><td><b>' + v + '</b></td></tr>').join('');
const t = s.tokens || {};
const html = '<div style="font-family:Arial,sans-serif;max-width:640px">'
  + '<h3>Audio AI-QA: звіт готовий</h3>'
  + '<p>Запуск <b>' + cfg.run_id + '</b>, модель ' + cfg.model + ', мова ' + cfg.language + '.</p>'
  + '<table>' + rows + '</table>'
  + '<p>Токени: вхід ' + t.prompt + ', вихід ' + t.output + ', thinking ' + t.thoughts + '.</p>'
  + '<p>CSV: gs://' + cfg.bucket + '/' + $('Звіт').first().json.report_object + '</p></div>';
return [{ json: { html, subject: 'Audio AI-QA: ' + (s['verdict:BRAK'] || 0) + ' BRAK з ' + s.total + ' (' + cfg.run_id + ')' } }];
"""

MAIL_ERR_JS = r"""
const st = $input.first().json;
const cfg = $('Налаштування').first().json;
const step = st.stage === '2' ? 'порівняння (крок 2)' : 'транскрипції (крок 1)';
const msg = st.phase === 'timeout'
  ? 'Задача Vertex ' + step + ' не завершилась за ' + cfg.max_wait_hours + ' год (стан ' + st.state + '), її скасовано.'
  : 'Задача Vertex ' + step + ' завершилась зі станом ' + st.state + (st.error ? ': ' + st.error : '') + '.';
return [{ json: { message: msg, job_name: st.job_name, subject: 'Audio AI-QA: помилка запуску ' + cfg.run_id } }];
"""

CLEANUP_JS = r"""
if (!$('Налаштування').first().json.delete_batch_input) return [];  // вимкнено в налаштуваннях
const run = $('Зіставлення і пакет').first().json.run;
const out = [{ json: { name: run + '/batch1.jsonl' } }];
if ($('Транскрипції і пакет 2').first().json.judge_requests > 0) out.push({ json: { name: run + '/batch2.jsonl' } });
return out;
"""

# --- підфлоу «Задача Vertex»

JOB_STATE_JS = r"""
const inp = $('Вхід').first().json;
const job = $input.first().json;
const created = $('Vertex: створити задачу').first().json;
const state = job.state || '';
const hours = (Date.now() - new Date(job.createTime || created.createTime).getTime()) / 3.6e6;
let phase = 'wait';
if (['JOB_STATE_SUCCEEDED', 'JOB_STATE_PARTIALLY_SUCCEEDED'].includes(state)) phase = 'done';
else if (['JOB_STATE_FAILED', 'JOB_STATE_CANCELLED', 'JOB_STATE_CANCELLING', 'JOB_STATE_EXPIRED'].includes(state)) phase = 'failed';
else if (hours > Number(inp.max_wait_hours)) phase = 'timeout';
const outDir = (job.outputInfo || {}).gcsOutputDirectory || '';
return [{ json: { stage: String(inp.stage), phase, state, hours: Math.round(hours * 10) / 10, job_name: job.name || created.name,
  error: job.error ? (job.error.message || JSON.stringify(job.error)) : '',
  out_prefix: outDir.replace('gs://' + inp.bucket + '/', '').replace(/\/?$/, '/'), stats: job.completionStats || {} } }];
"""

JOB_RESULT_FILES_JS = r"""
const out = [];
for (const it of $input.all()) for (const o of (it.json.items || [])) if (/\.jsonl$/.test(o.name)) out.push({ json: { name: o.name } });
if (!out.length) throw new Error('Vertex не записав жодного файлу результатів.');
return out;
"""

JOB_OK_JS = r"""
const st = $('Стан задачі').first().json;
return $input.all().map(i => ({ json: { ok: true, stage: st.stage, job_name: st.job_name, stats: st.stats, data: i.json.data } }));
"""

JOB_FAIL_JS = r"""
return [{ json: Object.assign({ ok: false }, $('Стан задачі').first().json) }];
"""

JOB_INPUTS = ["stage", "gcp_project", "gcp_location", "bucket", "model", "input_object", "output_prefix",
              "display_name", "poll_minutes", "max_wait_hours"]


def gcs_obj(bucket_expr, obj_expr):
    return "=https://storage.googleapis.com/storage/v1/b/" + bucket_expr + "/o/{{ encodeURIComponent(" + obj_expr + ") }}"


def http_factory(w):
    def http(name, params, pos, **kw):
        flags = dict(retryOnFail=True, maxTries=3, waitBetweenTries=10000)
        flags.update(kw)
        return w.add(name, "n8n-nodes-base.httpRequest", 4.2, dict(GCP_HTTP_AUTH, **params), pos, credentials=CRED_GCP, **flags)
    return http


def list_objects_params(bucket_expr, prefix_expr, extra=None):
    return {"url": "=https://storage.googleapis.com/storage/v1/b/" + bucket_expr + "/o",
            "sendQuery": True, "specifyQuery": "keypair",
            "queryParameters": {"parameters": [{"name": "prefix", "value": prefix_expr},
                                               {"name": "fields", "value": "items(name),nextPageToken"}] + (extra or [])},
            "options": {"pagination": {"pagination": {
                "paginationMode": "updateAParameterInEachRequest",
                "parameters": {"parameters": [{"type": "qs", "name": "pageToken", "value": "={{ $response.body.nextPageToken }}"}]},
                "paginationCompleteWhen": "other", "completeExpression": "={{ !$response.body.nextPageToken }}",
                "limitPagesFetched": True, "maxRequests": 1000}}}}


def build_vertex_job():
    """Підфлоу: одна задача Vertex AI Batch від запуску до результатів (або помилки з уже скасованою задачею)."""
    w = WF("vertexjob", "[Audio AI-QA] B. Задача Vertex (підфлоу)")
    x, y = 0, 300
    I = "$('Вхід').first().json"
    B = "{{ " + I + ".bucket }}"
    http = http_factory(w)
    sticky(w, "Опис підфлоу", (
        "## Підфлоу: одна задача Vertex AI Batch Prediction\n"
        "Викликається з «B. Пакетна перевірка» двічі: крок 1 (транскрипція) і крок 2 (порівняння).\n"
        "Вхід: файл JSONL у бакеті, префікс для результатів, параметри проєкту, інтервал опитування, таймаут.\n"
        "Вихід: елементи `{ok: true, data}` з текстом кожного файлу результатів, або один `{ok: false, phase, state, error}`.\n"
        "При FAILED/CANCELLED/EXPIRED або таймауті задача скасовується тут же, рішення (лист, зупинка) за основним флоу."),
        (x - 40, y - 260), 820, 220)
    t = w.add("Вхід", "n8n-nodes-base.executeWorkflowTrigger", 1.1,
              {"inputSource": "workflowInputs", "workflowInputs": {"values": [
                  {"name": n, "type": "number" if n in ("poll_minutes", "max_wait_hours") else "string"} for n in JOB_INPUTS]}},
              (x, y))
    job = http("Vertex: створити задачу", {
        "method": "POST",
        "url": "=https://{{ " + I + ".gcp_location }}-aiplatform.googleapis.com/v1/projects/{{ " + I + ".gcp_project }}/locations/{{ " + I + ".gcp_location }}/batchPredictionJobs",
        "sendBody": True, "specifyBody": "json",
        "jsonBody": ("={{ JSON.stringify({ displayName: " + I + ".display_name, "
                     "model: 'publishers/google/models/' + " + I + ".model, "
                     "inputConfig: { instancesFormat: 'jsonl', gcsSource: { uris: ['gs://' + " + I + ".bucket + '/' + " + I + ".input_object] } }, "
                     "outputConfig: { predictionsFormat: 'jsonl', gcsDestination: { outputUriPrefix: 'gs://' + " + I + ".bucket + '/' + " + I + ".output_prefix } } }) }}"),
        "options": {}}, (x + 220, y))
    wt = w.add("Чекати N хвилин", "n8n-nodes-base.wait", 1.1,
               {"amount": "={{ " + I + ".poll_minutes }}", "unit": "minutes"}, (x + 440, y), webhookId=nid("vertexjob", "wait-hook"))
    gs = http("Vertex: стан задачі", {
        "url": "=https://{{ " + I + ".gcp_location }}-aiplatform.googleapis.com/v1/{{ $('Vertex: створити задачу').first().json.name }}",
        "options": {}}, (x + 660, y), maxTries=5, waitBetweenTries=30000)
    stt = w.add("Стан задачі", "n8n-nodes-base.code", 2, code(JOB_STATE_JS, False), (x + 880, y))
    i_done = w.add("Готово?", "n8n-nodes-base.if", 2.2, if_eq("={{ $json.phase }}", "done"), (x + 1100, y))
    i_wait = w.add("Ще чекати?", "n8n-nodes-base.if", 2.2, if_eq("={{ $json.phase }}", "wait"), (x + 1320, y + 220))
    lr = http("GCS: список результатів", list_objects_params(B, "={{ $json.out_prefix }}"), (x + 1320, y - 20), executeOnce=True)
    rf = w.add("Файли результатів", "n8n-nodes-base.code", 2, code(JOB_RESULT_FILES_JS, False), (x + 1540, y - 20))
    dr = http("GCS: завантажити результат", {
        "url": gcs_obj(B, "$json.name") + "?alt=media",
        "options": {"timeout": 600000, "response": {"response": {"responseFormat": "text", "outputPropertyName": "data"}}}},
        (x + 1760, y - 20))
    ok = w.add("Результат: успіх", "n8n-nodes-base.code", 2, code(JOB_OK_JS, False), (x + 1980, y - 20))
    cancel = http("Vertex: скасувати задачу", {
        "method": "POST",
        "url": "=https://{{ " + I + ".gcp_location }}-aiplatform.googleapis.com/v1/{{ $json.job_name }}:cancel",
        "options": {}}, (x + 1540, y + 400), onError="continueRegularOutput", retryOnFail=False)
    fail = w.add("Результат: помилка", "n8n-nodes-base.code", 2, code(JOB_FAIL_JS, False), (x + 1760, y + 400))
    w.chain(t, job, wt, gs, stt, i_done)
    w.link(i_done, lr, 0)
    w.link(i_done, i_wait, 1)
    w.link(i_wait, wt, 0)
    w.link(i_wait, cancel, 1)
    w.chain(cancel, fail)
    w.chain(lr, rf, dr, ok)
    return apply_layout(w, (
        "# Audio AI-QA · B. Підфлоу: одна задача Vertex AI Batch\n"
        "Викликається двічі з «B. Пакетна перевірка». Вихід: {ok: true, data} на кожен файл результатів\n"
        "або {ok: false, phase, state, error} після скасування задачі."),
        [(0, 7, "1. Запуск і очікування", "задача → чекати N хв → стан", [t, job, wt, gs, stt, i_done]),
         (0, 5, "2. Результати", "файли predictions з бакета", [lr, rf, dr, ok]),
         (1, 3, "Збій або таймаут", "ще чекати? ні → скасувати → повернути помилку", [i_wait, cancel, fail])])


STEP, PAD, NODE_W, HEADER, FRAME_H, ROW_H, GAP = 220, 70, 100, 170, 380, 470, 50


def apply_layout(w, title, blocks):
    """Розкладка блоками: blocks = [(рядок, колір, заголовок, підзаголовок, [імена вузлів])].
    Блоки одного рядка йдуть зліва направо, кожен у кольоровій рамці однакової висоти."""
    pos = {}
    frames = []
    cursor = {}
    for row, color, head, sub, names in blocks:
        x0 = cursor.get(row, 0)
        y = 120 + row * ROW_H
        width = PAD * 2 + (len(names) - 1) * STEP + NODE_W
        for i, n in enumerate(names):
            pos[n] = [x0 + PAD + i * STEP, y + HEADER]
        frames.append((x0, y, width, head, sub, color))
        cursor[row] = x0 + width + GAP
    total_w = max(cursor.values()) - GAP
    for n in w.nodes:
        if n["name"] in pos:
            n["position"] = pos[n["name"]]
    missing = [n["name"] for n in w.nodes if n["type"] != "n8n-nodes-base.stickyNote" and n["name"] not in pos]
    assert not missing, "без місця в розкладці: " + ", ".join(missing)
    w.nodes = [n for n in w.nodes if n["type"] != "n8n-nodes-base.stickyNote"]
    w.add("Опис флоу", "n8n-nodes-base.stickyNote", 1, {"content": title, "width": total_w, "height": 50 + 28 * (title.count(chr(10)) + 1), "color": 1}, (0, 0))
    for k, (x0, y, width, head, sub, color) in enumerate(frames):
        w.add("Рамка " + str(k + 1), "n8n-nodes-base.stickyNote", 1,
              {"content": "## " + head + "\n" + sub, "width": width, "height": FRAME_H, "color": color}, (x0, y))
    # рамки позаду вузлів: n8n малює стікери в порядку списку, тож переносимо їх на початок
    w.nodes = [n for n in w.nodes if n["type"] == "n8n-nodes-base.stickyNote"] + \
              [n for n in w.nodes if n["type"] != "n8n-nodes-base.stickyNote"]
    return w.json()


def build_batch():
    w = WF("batch", "[Audio AI-QA] B. Пакетна перевірка (GCS + Vertex AI Batch)")
    B = "{{ $('Налаштування').first().json.bucket }}"
    P = "$('Налаштування').first().json"
    RUN = "$('Зіставлення і пакет').first().json.run"
    http = http_factory(w)
    o = (0, 0)  # позиції задає apply_layout
    t = w.add("Ручний запуск", "n8n-nodes-base.manualTrigger", 1, {}, o)
    c = w.add("Налаштування", "n8n-nodes-base.set", 3.4, setcfg({
        "gcp_project": ("<GCP_PROJECT_ID>", "string"),
        "gcp_location": ("us-central1", "string"),
        "bucket": ("<GCS_BUCKET>", "string"),
        "csv_object": ("Bahasa/dataset.csv", "string"),
        "audio_prefix": ("Bahasa/audio/", "string"),
        "output_prefix": ("ai-qa-runs", "string"),
        "language": ("Indonesian", "string"),
        "script": ("Latin", "string"),
        "model": ("gemini-2.5-flash", "string"),
        "thinking_budget": ("0", "string"),
        "poll_minutes": (10, "number"),
        "max_wait_hours": (24, "number"),
        "notify_email": ("<NOTIFY_EMAIL>", "string"),
        "delete_batch_input": (True, "boolean"),
        "run_id": ("={{ $now.toFormat('yyyyLLdd-HHmmss') }}", "string"),
    }), o)

    def upload(name, field, obj):
        return http(name, {
            "method": "POST", "url": "=https://storage.googleapis.com/upload/storage/v1/b/" + B + "/o",
            "sendQuery": True, "specifyQuery": "keypair",
            "queryParameters": {"parameters": [{"name": "uploadType", "value": "media"}, {"name": "name", "value": obj}]},
            "sendBody": True, "contentType": "binaryData", "inputDataFieldName": field, "options": {"timeout": 600000}}, o)

    def download_text(name, obj_expr):
        n = http(name, {"url": gcs_obj(B, obj_expr) + "?alt=media",
                        "options": {"timeout": 600000, "response": {"response": {"responseFormat": "text", "outputPropertyName": "data"}}}},
                 o, executeOnce=True)
        return n

    def vertex_step(k):
        vals = {"stage": k, "display_name": "={{ 'audio-ai-qa-" + k + "-' + " + P + ".run_id }}",
                "input_object": "={{ " + RUN + " + '/batch" + k + ".jsonl' }}",
                "output_prefix": "={{ " + RUN + " + '/results" + k + "/' }}"}
        for n in ("gcp_project", "gcp_location", "bucket", "model", "poll_minutes", "max_wait_hours"):
            vals[n] = "={{ " + P + "." + n + " }}"
        schema = [{"id": n, "displayName": n, "required": False, "defaultMatch": False, "display": True, "canBeUsedToMatch": True,
                   "type": "number" if n in ("poll_minutes", "max_wait_hours") else "string"} for n in JOB_INPUTS]
        return w.add("Крок " + k + ": задача Vertex", "n8n-nodes-base.executeWorkflow", 1.2, {
            "mode": "once", "source": "database",
            "workflowId": {"__rl": True, "mode": "id", "value": "<VERTEX_JOB_WORKFLOW_ID>"},
            "workflowInputs": {"mappingMode": "defineBelow", "value": vals, "matchingColumns": [], "schema": schema,
                               "attemptToConvertTypes": False, "convertFieldsToString": True},
            "options": {"waitForSubWorkflow": True}}, o)

    dcsv = http("GCS: завантажити CSV", {"url": gcs_obj(B, P + ".csv_object") + "?alt=media",
                                          "options": {"response": {"response": {"responseFormat": "file"}}}}, o)
    ex = w.add("CSV у рядки", "n8n-nodes-base.extractFromFile", 1, {"options": {}}, o)
    lst = http("GCS: список аудіо", list_objects_params(B, "={{ " + P + ".audio_prefix }}", [{"name": "maxResults", "value": "1000"}]),
               o, executeOnce=True)
    mt = w.add("Зіставлення і пакет", "n8n-nodes-base.code", 2, code(MATCH_B_JS), o)
    up1 = upload("GCS: зберегти пакет 1", "data", "={{ $json.object }}")
    s1 = vertex_step("1")
    ok1 = w.add("Крок 1 успішний?", "n8n-nodes-base.if", 2.2, if_eq("={{ $json.ok }}", "", op="true", typ="boolean"), o, executeOnce=True)
    rm = download_text("GCS: завантажити rows.json", RUN + " + '/rows.json'")
    s2c = w.add("Транскрипції і пакет 2", "n8n-nodes-base.code", 2, code(STAGE2_JS), o)
    up2 = upload("GCS: зберегти пакет 2", "data", "={{ $json.object }}")
    need2 = w.add("Є що порівнювати?", "n8n-nodes-base.if", 2.2,
                  if_eq("={{ $('Транскрипції і пакет 2').first().json.judge_requests }}", 0, op="gt", typ="number"), o, executeOnce=True)
    s2 = vertex_step("2")
    ok2 = w.add("Крок 2 успішний?", "n8n-nodes-base.if", 2.2, if_eq("={{ $json.ok }}", "", op="true", typ="boolean"), o, executeOnce=True)
    gi = download_text("GCS: завантажити interim.json", RUN + " + '/interim.json'")
    rp = w.add("Звіт", "n8n-nodes-base.code", 2, code(REPORT_JS), o)
    ur = upload("GCS: зберегти report.csv", "csv", "={{ $json.report_object }}")
    mo = w.add("Текст листа", "n8n-nodes-base.code", 2, code(MAIL_OK_JS, False), o)
    gm = w.add("Gmail: звіт готовий", "n8n-nodes-base.gmail", 2.1, {
        "sendTo": "={{ " + P + ".notify_email }}", "subject": "={{ $json.subject }}",
        "message": "={{ $json.html }}", "options": {"appendAttribution": False}},
        o, credentials=CRED_GMAIL, webhookId=nid("batch", "gmail-ok"), onError="continueRegularOutput")
    cl = w.add("Вхідні пакети", "n8n-nodes-base.code", 2, code(CLEANUP_JS, False), o)
    dl = http("GCS: видалити пакет", {"method": "DELETE", "url": gcs_obj(B, "$json.name"), "options": {}},
              o, onError="continueRegularOutput")
    me = w.add("Текст помилки", "n8n-nodes-base.code", 2, code(MAIL_ERR_JS, False), o)
    ge = w.add("Gmail: помилка", "n8n-nodes-base.gmail", 2.1, {
        "sendTo": "={{ " + P + ".notify_email }}", "subject": "={{ $json.subject }}",
        "emailType": "text", "message": "={{ $json.message }}", "options": {"appendAttribution": False}},
        o, credentials=CRED_GMAIL, webhookId=nid("batch", "gmail-err"), onError="continueRegularOutput")
    stop = w.add("Зупинити з помилкою", "n8n-nodes-base.stopAndError", 1,
                 {"errorMessage": "={{ $('Текст помилки').first().json.message }}"}, o)

    w.chain(t, c, dcsv, ex, lst, mt, up1, s1, ok1)
    w.link(ok1, rm, 0)
    w.link(ok1, me, 1)
    w.chain(rm, s2c, up2, need2)
    w.link(need2, s2, 0)
    w.link(need2, gi, 1)
    w.chain(s2, ok2)
    w.link(ok2, gi, 0)
    w.link(ok2, me, 1)
    w.chain(gi, rp, ur, mo, gm, cl, dl)
    w.chain(me, ge, stop)
    return apply_layout(w, (
        "# Audio AI-QA · B. Пакетна перевірка всього датасету\n"
        "GCS + Vertex AI Batch, без SSH і ключів на диску. Задачі Vertex виконує підфлоу «B. Задача Vertex»."),
        [(0, 7, "1. Підготовка", "CSV і аудіо з GCS → зіставлення → пакет кроку 1", [t, c, dcsv, ex, lst, mt, up1]),
         (0, 4, "2. Крок 1", "сліпа транскрипція", [s1, ok1]),
         (1, 6, "3. Правила", "тиша = BRAK, збіг = OK, решта → крок 2", [rm, s2c, up2, need2]),
         (1, 4, "4. Крок 2", "порівняння текстів", [s2, ok2]),
         (1, 3, "Помилка", "лист і зупинка", [me, ge, stop]),
         (2, 5, "5. Звіт", "CSV у бакет, лист, прибирання", [gi, rp, ur, mo, gm, cl, dl])])


def apply_private(wf, priv):
    s = json.dumps(wf, ensure_ascii=False)
    for k, v in priv.items():
        s = s.replace("<" + k + ">", str(v))
    return json.loads(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--private", help="JSON з реальними значеннями заглушок (не комітити)")
    ap.add_argument("--out", default=str(ROOT / "workflows"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    priv = json.loads(Path(a.private).read_text(encoding="utf-8")) if a.private else {}
    for fname, wf in (("audio-qa-sync.json", build_sync()), ("audio-qa-batch-vertex.json", build_batch()),
                     ("audio-qa-vertex-job.json", build_vertex_job())):
        if priv:
            wf = apply_private(copy.deepcopy(wf), priv)
        (out / fname).write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("written", out / fname, len(wf["nodes"]), "nodes")


if __name__ == "__main__":
    main()
