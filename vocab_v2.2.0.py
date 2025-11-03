import csv
import html
import os
import random
import sqlite3
import sys
from datetime import datetime

from PyQt5 import QtCore
from PyQt5 import QtWidgets, QtGui

DB_PATH = os.path.join(os.path.expanduser("~"), "vocab_trainer_units_v2.db")

# --------------------------
# 数据库初始化
# --------------------------
def init_db(path=DB_PATH):
    first = not os.path.exists(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY,
        language TEXT NOT NULL,
        unit TEXT DEFAULT '',
        term TEXT NOT NULL,
        meaning TEXT,
        created_at TEXT,
        last_review TEXT,
        interval INTEGER DEFAULT 0,
        repetition INTEGER DEFAULT 0,
        ef REAL DEFAULT 2.5,
        due_date TEXT
    )
    ''')
    conn.commit()
    cols = [c[1] for c in cur.execute('PRAGMA table_info(cards)').fetchall()]
    # 兼容老库：补充缺失列（新列追加在表末尾，不影响旧索引）
    if 'unit' not in cols:
        try:
            cur.execute('ALTER TABLE cards ADD COLUMN unit TEXT DEFAULT ""')
        except Exception:
            pass
    cols = [c[1] for c in cur.execute('PRAGMA table_info(cards)').fetchall()]
    if 'jp_kanji' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN jp_kanji TEXT')
    if 'jp_kana' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN jp_kana TEXT')
    if 'jp_ruby' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN jp_ruby TEXT')
    conn.commit()
    return conn

# --------------------------
# DB 操作
# --------------------------
def add_card(conn, language, unit, term, meaning, jp_kanji=None, jp_kana=None, jp_ruby=None):
    now = datetime.utcnow().isoformat()
    due = None
    cur = conn.cursor()
    cur.execute('''
    INSERT INTO cards (
        language, unit, term, meaning,
        created_at, last_review, interval, repetition, ef, due_date,
        jp_kanji, jp_kana, jp_ruby
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (language, unit, term, meaning,
          now, None, 0, 0, 2.5, due,
          jp_kanji, jp_kana, jp_ruby))
    conn.commit()
    return cur.lastrowid

def list_units(conn):
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT unit FROM cards ORDER BY unit')
    rows = [r[0] for r in cur.fetchall() if r[0] and r[0] != ""]
    return rows

def list_cards_by_unit(conn, unit=None):
    cur = conn.cursor()
    if unit is None or unit == "" or unit == "所有单元":
        cur.execute('SELECT * FROM cards ORDER BY id')
    else:
        cur.execute('SELECT * FROM cards WHERE unit = ? ORDER BY id', (unit,))
    return cur.fetchall()

def list_due_cards(conn, unit=None):
    cur = conn.cursor()
    if unit and unit != "" and unit != "所有单元":
        cur.execute('SELECT * FROM cards WHERE unit = ? ORDER BY id', (unit,))
    else:
        cur.execute('SELECT * FROM cards ORDER BY id')
    return cur.fetchall()

def update_card_review(conn, card_id, interval, repetition, ef, last_review, due_date):
    cur = conn.cursor()
    cur.execute('''
    UPDATE cards
    SET interval = ?, repetition = ?, ef = ?, last_review = ?, due_date = ?
    WHERE id = ?
    ''', (interval, repetition, ef, last_review, due_date, card_id))
    conn.commit()

def update_card_fields(conn, card_id, language, unit, term, meaning):
    cur = conn.cursor()
    cur.execute('''
    UPDATE cards
    SET language = ?, unit = ?, term = ?, meaning = ?
    WHERE id = ?
    ''', (language, unit, term, meaning, card_id))
    conn.commit()

def delete_card(conn, card_id):
    cur = conn.cursor()
    cur.execute('DELETE FROM cards WHERE id = ?', (card_id,))
    conn.commit()

# 1) 顶部 DB 操作区域附近（已有 import csv），新增/替换：完整导出函数
def export_all_cards_to_csv(conn, filepath: str):
    """
    导出 cards 全表为 CSV（含学习记录），UTF-8 BOM 防止 Excel 乱码。
    按当前表结构动态输出列，兼容老/新库（如 jp_kanji/jp_kana/jp_ruby 可选）。
    """
    cur = conn.cursor()
    cols_info = cur.execute('PRAGMA table_info(cards)').fetchall()
    if not cols_info:
        raise RuntimeError("数据库中不存在表 cards")

    # 按表定义顺序输出列
    col_names = [c[1] for c in cols_info]

    # 友好表头映射（无映射则保留原名）
    name_map = {
        "id": "ID",
        "language": "语言",
        "unit": "单元",
        "term": "日语词条",
        "meaning": "中文释义",
        "interval": "间隔",
        "repetition": "重复次数",
        "ef": "易度",
        "last_review": "上次复习时间",
        "due_date": "到期时间",
        "created_at": "创建时间",
        "jp_kanji": "汉字写法",
        "jp_kana": "假名读音",
        "jp_ruby": "读音标注",
    }
    header = [name_map.get(n, n) for n in col_names]

    rows = cur.execute(f'SELECT {", ".join(col_names)} FROM cards ORDER BY unit, id').fetchall()

    # 写 CSV：UTF-8 BOM，避免 Excel 乱码；newline='' 防止空行
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for r in rows:
            writer.writerow(["" if v is None else v for v in r])


class LocalJaZhDict:
    def __init__(self, path: str | None = None):
        self.path = (path or "").strip()
        self._loaded = False
        self._map: dict[str, str] = {}                 # key -> meaning
        self._entry: dict[str, tuple[str,str,str]] = {}# key -> (term, kana, meaning)

    @staticmethod
    def _norm_key(s: str) -> str:
        if not s:
            return ""
        s = s.replace("\r", "").replace("\n", " ").strip()
        s = s.replace("\u3000", " ")
        s = s.replace("～", "~")
        s = s.replace("［", "").replace("］", "")
        s = s.replace("（", "(").replace("）", ")")
        while "  " in s:
            s = s.replace("  ", " ")
        return s

    @staticmethod
    def _clean_path(p: str) -> str:
        p = (p or "").strip().strip("'\"")
        if not p:
            return ""
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.abspath(p)
        return p

    @staticmethod
    def _is_kana(s: str) -> bool:
        if not s:
            return False
        for ch in s:
            code = ord(ch)
            if ch.isspace():
                continue
            if (
                0x3040 <= code <= 0x309F or  # ひらがな
                0x30A0 <= code <= 0x30FF or  # カタカナ
                0x31F0 <= code <= 0x31FF or  # 小假名
                0xFF66 <= code <= 0xFF9D or  # 半角片假名
                ch in ("ー", "・")
            ):
                continue
            return False
        return True

    def load(self, path: str | None = None):
        if self._loaded and not path:
            return
        p = self._clean_path(path or self.path)
        self.path = p
        self._loaded = True
        self._map = {}
        self._entry = {}
        if not p or not os.path.exists(p):
            print(f"[LocalJaZhDict] 未找到词典文件: {p}")
            return

        def _store_entry(m_mean: dict[str,str], m_entry: dict[str,tuple[str,str,str]], term: str, kana: str, meaning: str):
            # 建立多键索引：原样与规范化；包含 term 与 kana（若合法）
            keys = []
            if term:
                keys += [term, self._norm_key(term)]
            if kana and self._is_kana(kana):
                keys += [kana, self._norm_key(kana)]
            # 去空去重
            keys = [k for k in dict.fromkeys([k for k in keys if k])]
            for k in keys:
                m_mean[k] = meaning
                m_entry[k] = (term, kana, meaning)

        def _read_csv(encoding: str) -> tuple[dict[str,str], dict[str,tuple[str,str,str]]]:
            m_mean: dict[str, str] = {}
            m_entry: dict[str, tuple[str, str, str]] = {}
            with open(p, "r", encoding=encoding, newline="") as f:
                rd = csv.reader(f)
                for i, row in enumerate(rd):
                    if not row:
                        continue
                    # 跳过表头
                    if i == 0 and row[0].strip().lower() in ("term", "词条"):
                        continue
                    # 兼容 2/3 列：term[,kana],meaning
                    if len(row) < 2:
                        continue
                    term = (row[0] or "").strip()
                    kana = (row[1] or "").strip() if len(row) >= 3 else ""
                    meaning = (row[2] if len(row) >= 3 else row[1] or "").strip()
                    if not term or not meaning:
                        continue
                    _store_entry(m_mean, m_entry, term, kana, meaning)
            return m_mean, m_entry

        try:
            m1, e1 = _read_csv("utf-8-sig")
            self._map, self._entry = m1, e1
            print(f"[LocalJaZhDict] 已加载 {len(self._entry)} 键，文件: {p}")
        except UnicodeDecodeError:
            try:
                m2, e2 = _read_csv("utf-8")
                self._map, self._entry = m2, e2
                print(f"[LocalJaZhDict] 已加载 {len(self._entry)} 键（utf-8），文件: {p}")
            except Exception as e:
                print(f"[LocalJaZhDict] 加载失败: {e}")
                self._map, self._entry = {}, {}
        except Exception as e:
            print(f"[LocalJaZhDict] 加载失败: {e}")
            self._map, self._entry = {}, {}

    def get(self, term: str) -> str:
        if not self._loaded:
            self.load()
        t = (term or "").strip()
        if not t:
            return ""
        if t in self._map:
            return self._map[t]
        t2 = self._norm_key(t)
        return self._map.get(t2, "")

    def get_full(self, key: str) -> tuple[str, str, str] | None:
        if not self._loaded:
            self.load()
        k = (key or "").strip()
        if not k:
            return None
        if k in self._entry:
            return self._entry[k]
        k2 = self._norm_key(k)
        return self._entry.get(k2, None)


# --- 2) 模块级：初始化与帮助函数（保持已有 suggest_zh_meaning，仅新增 suggest_full_entry） ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCAL_CSV = os.path.join(BASE_DIR, "tools", "vocab_local_jazh.csv")

_LOCAL_DICT = LocalJaZhDict(os.environ.get("LOCAL_JAZH_CSV") or DEFAULT_LOCAL_CSV)
_LOCAL_DICT.load()

def _get_local_meaning(term: str) -> str:
    return _LOCAL_DICT.get(term)

def suggest_zh_meaning(term: str) -> str:
    term = (term or "").strip()
    if not term:
        return ""
    return _get_local_meaning(term) or ""

def suggest_full_entry(key: str) -> tuple[str, str, str] | None:
    # 返回 (term, kana, meaning) 或 None
    return _LOCAL_DICT.get_full(key or "")

# --------------------------
# SM-2 算法（保持不变）
# --------------------------
def sm2_update(card_row, quality):
    interval = card_row[7]
    repetition = card_row[8]
    ef = card_row[9]
    q = quality
    if q < 3:
        repetition = 0
        interval = 1
    else:
        repetition += 1
        if repetition == 1:
            interval = 1
        elif repetition == 2:
            interval = 6
        else:
            interval = max(1, int(round(interval * ef)))
    ef = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    if ef < 1.3:
        ef = 1.3
    return interval, repetition, ef

# 4) 帮助函数：拼显示 & 答案集合 ---------------------------------------
def format_jp_term(row):
    # 旧库：行长 11；新库：>=14
    jp_kanji = row[11] if len(row) > 11 else None
    jp_kana  = row[12] if len(row) > 12 else None
    if (jp_kanji and jp_kanji.strip()) and (jp_kana and jp_kana.strip()):
        return f"{jp_kanji}｜{jp_kana}"
    if jp_kanji and jp_kanji.strip():
        return jp_kanji
    if jp_kana and jp_kana.strip():
        return jp_kana
    return row[3]  # 回退到 term

def jp_answer_set(row):
    s = set()
    s.add((row[3] or "").strip())
    if len(row) > 11:
        if row[11]: s.add(row[11].strip())
        if row[12]: s.add(row[12].strip())
    # 去空
    return {x for x in s if x}

def norm(s):
    return (s or "").strip()

# 1) 新增两个辅助函数（放在已有的 format_jp_term/jp_answer_set/norm 之后）
def format_jp_term_for_table(row):
    # 索引：id, language, unit, term, meaning, ..., jp_kanji(11), jp_kana(12)
    term = (row[3] or "").strip()
    jp_kanji = row[11].strip() if len(row) > 11 and row[11] else ""
    return f"{term} | {jp_kanji}" if term and jp_kanji else term or jp_kanji

# 保持“假名读音”列为 jp_kana
def get_jp_kanji(row):
    return (row[11] or "").strip() if len(row) > 11 and row[11] else ""

def get_jp_kana(row):
    return (row[12] or "").strip() if len(row) > 12 and row[12] else ""

# --- 4) 替换 MainWindow._auto_fill_meaning_from_term：命中时同时联动“汉字写法/日语词条/中文释义” ---
def _has_kana(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF:
            return True
    return False

def _has_kanji(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            return True
    return False

def _kata_to_hira(s: str) -> str:
    out = []
    for ch in s or "":
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)

def kana_to_romaji(kana: str) -> str:
    if not kana:
        return ""
    k = _kata_to_hira(kana)
    digraph = {
        "きゃ":"kya","きゅ":"kyu","きょ":"kyo",
        "ぎゃ":"gya","ぎゅ":"gyu","ぎょ":"gyo",
        "しゃ":"sha","しゅ":"shu","しょ":"sho",
        "じゃ":"ja","じゅ":"ju","じょ":"jo",
        "ちゃ":"cha","ちゅ":"chu","ちょ":"cho",
        "にゃ":"nya","にゅ":"nyu","にょ":"nyo",
        "ひゃ":"hya","ひゅ":"hyu","ひょ":"hyo",
        "びゃ":"bya","びゅ":"byu","びょ":"byo",
        "ぴゃ":"pya","ぴゅ":"pyu","ぴょ":"pyo",
        "みゃ":"mya","みゅ":"myu","みょ":"myo",
        "りゃ":"rya","りゅ":"ryu","りょ":"ryo",
        "ゔぁ":"va","ゔぃ":"vi","ゔぇ":"ve","ゔぉ":"vo",
    }
    mono = {
        "あ":"a","い":"i","う":"u","え":"e","お":"o",
        "か":"ka","き":"ki","く":"ku","け":"ke","こ":"ko",
        "が":"ga","ぎ":"gi","ぐ":"gu","げ":"ge","ご":"go",
        "さ":"sa","し":"shi","す":"su","せ":"se","そ":"so",
        "ざ":"za","じ":"ji","ず":"zu","ぜ":"ze","ぞ":"zo",
        "た":"ta","ち":"chi","つ":"tsu","て":"te","と":"to",
        "だ":"da","ぢ":"ji","づ":"zu","で":"de","ど":"do",
        "な":"na","に":"ni","ぬ":"nu","ね":"ne","の":"no",
        "は":"ha","ひ":"hi","ふ":"fu","へ":"he","ほ":"ho",
        "ば":"ba","び":"bi","ぶ":"bu","べ":"be","ぼ":"bo",
        "ぱ":"pa","ぴ":"pi","ぷ":"pu","ぺ":"pe","ぽ":"po",
        "ま":"ma","み":"mi","む":"mu","め":"me","も":"mo",
        "や":"ya","ゆ":"yu","よ":"yo",
        "ら":"ra","り":"ri","る":"ru","れ":"re","ろ":"ro",
        "わ":"wa","を":"o","ん":"n",
        "ぁ":"a","ぃ":"i","ぅ":"u","ぇ":"e","ぉ":"o",
        "ー":"-","ゔ":"vu","ゎ":"wa","ゕ":"ka","ゖ":"ka",
        "っ":""  # 促音单独不输出，由后续首辅音加倍
    }
    res = []
    i = 0
    sokuon = False
    while i < len(k):
        ch = k[i]
        if ch == "っ":
            sokuon = True
            i += 1
            continue
        token = None
        if i + 1 < len(k):
            pair = k[i:i+2]
            if pair in digraph:
                token = digraph[pair]
                i += 2
        if token is None:
            token = mono.get(ch, ch)
            i += 1
        if sokuon and token and token[0] in "bcdfghjklmnpqrstvwxyz":
            token = token[0] + token
        sokuon = False
        res.append(token)
    out = []
    last_vowel = ""
    vowels = "aeiou"
    for token in res:
        if token == "-":
            out.append(last_vowel if last_vowel else "")
            continue
        out.append(token)
        for c in reversed(token):
            if c in vowels:
                last_vowel = c
                break
    return "".join(out)

def _kata_to_hira(s: str) -> str:
    out = []
    for ch in s:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:  # Katakana ァ..ヶ
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)

def kana_to_romaji(kana: str) -> str:
    if not kana:
        return ""
    k = _kata_to_hira(kana)

    digraph = {
        "きゃ":"kya","きゅ":"kyu","きょ":"kyo",
        "ぎゃ":"gya","ぎゅ":"gyu","ぎょ":"gyo",
        "しゃ":"sha","しゅ":"shu","しょ":"sho",
        "じゃ":"ja","じゅ":"ju","じょ":"jo",
        "ちゃ":"cha","ちゅ":"chu","ちょ":"cho",
        "にゃ":"nya","にゅ":"nyu","にょ":"nyo",
        "ひゃ":"hya","ひゅ":"hyu","ひょ":"hyo",
        "びゃ":"bya","びゅ":"byu","びょ":"byo",
        "ぴゃ":"pya","ぴゅ":"pyu","ぴょ":"pyo",
        "みゃ":"mya","みゅ":"myu","みょ":"myo",
        "りゃ":"rya","りゅ":"ryu","りょ":"ryo",
        "ゔぁ":"va","ゔぃ":"vi","ゔぇ":"ve","ゔぉ":"vo",
    }
    mono = {
        "あ":"a","い":"i","う":"u","え":"e","お":"o",
        "か":"ka","き":"ki","く":"ku","け":"ke","こ":"ko",
        "が":"ga","ぎ":"gi","ぐ":"gu","げ":"ge","ご":"go",
        "さ":"sa","し":"shi","す":"su","せ":"se","そ":"so",
        "ざ":"za","じ":"ji","ず":"zu","ぜ":"ze","ぞ":"zo",
        "た":"ta","ち":"chi","つ":"tsu","て":"te","と":"to",
        "だ":"da","ぢ":"ji","づ":"zu","で":"de","ど":"do",
        "な":"na","に":"ni","ぬ":"nu","ね":"ne","の":"no",
        "は":"ha","ひ":"hi","ふ":"fu","へ":"he","ほ":"ho",
        "ば":"ba","び":"bi","ぶ":"bu","べ":"be","ぼ":"bo",
        "ぱ":"pa","ぴ":"pi","ぷ":"pu","ぺ":"pe","ぽ":"po",
        "ま":"ma","み":"mi","む":"mu","め":"me","も":"mo",
        "や":"ya","ゆ":"yu","よ":"yo",
        "ら":"ra","り":"ri","る":"ru","れ":"re","ろ":"ro",
        "わ":"wa","を":"o","ん":"n",
        "ぁ":"a","ぃ":"i","ぅ":"u","ぇ":"e","ぉ":"o",
        "ー":"-",
        "ゔ":"vu",
    }

    res = []
    i = 0
    sokuon = False  # っ
    while i < len(k):
        ch = k[i]
        if ch == "っ":
            sokuon = True
            i += 1
            continue

        token = None
        if i + 1 < len(k):
            pair = k[i:i+2]
            if pair in digraph:
                token = digraph[pair]
                i += 2
        if token is None:
            if ch in mono:
                token = mono[ch]
            else:
                token = ch
            i += 1

        if sokuon and token and token[0] in "bcdfghjklmnpqrstvwxyz":
            token = token[0] + token
        sokuon = False
        res.append(token)

    out = []
    last_vowel = ""
    vowels = "aeiou"
    for token in res:
        if token == "-":
            out.append(last_vowel if last_vowel else "-")
            continue
        out.append(token)
        for c in reversed(token):
            if c in vowels:
                last_vowel = c
                break
    return "".join(out)

def _has_kana(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        code = ord(ch)
        if 0x3041 <= code <= 0x309F or 0x30A0 <= code <= 0x30FF:
            return True
    return False

def _has_kanji(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            return True
    return False

def _is_romaji(s: str) -> bool:
    if not s:
        return False
    if _has_kana(s) or _has_kanji(s):
        return False
    try:
        s.encode("ascii")
        return True
    except Exception:
        return False

def pick_kana(row) -> str:
    jp_kana = get_jp_kana(row)
    if _has_kana(jp_kana):
        return jp_kana.strip()
    term = norm(row[3])
    return term if _has_kana(term) else ""

def pick_romaji(row, kana_text: str) -> str:
    jp_kana = get_jp_kana(row)
    if _is_romaji(jp_kana):
        return jp_kana.strip()
    return kana_to_romaji(kana_text) if kana_text else ""

# --- 2) 新增：按 id 读取完整卡片 ---
def get_card_by_id(conn, card_id):
    cur = conn.cursor()
    cur.execute('SELECT * FROM cards WHERE id = ?', (card_id,))
    return cur.fetchone()

# --- 3) 新增：完整字段更新（不影响你原来的 update_card_fields） ---
def update_card_fields_full(conn, card_id, language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby=None):
    cur = conn.cursor()
    cur.execute('''
        UPDATE cards
        SET language = ?, unit = ?, term = ?, meaning = ?,
            jp_kanji = ?, jp_kana = ?, jp_ruby = ?
        WHERE id = ?
    ''', (language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby, card_id))
    conn.commit()

# 5.5 parse_iso：补全占位
def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

# --------------------------
# UI 样式和窗口
# --------------------------
APP_STYLE = """
/* 全局 */
QWidget {
    font-family: "Microsoft YaHei", "PingFang SC", Arial;
    color: #1f2937; /* slate-800 */
    font-size: 14px;
}
QMainWindow { background: #f5f7fb; }

/* 标题 */
QLabel#appTitle {
    font-size: 20px; font-weight: 800; color: #111827;
}
QLabel#sectionTitle {
    font-size: 16px; font-weight: 700; color: #111827; margin: 4px 0 10px 0;
}
QLabel#muted {
    color:#6b7280; font-size: 12px;
}

/* 面板（卡片） */
QFrame#panel {
    background: #ffffff;
    border: 1px solid #e8edf5;
    border-radius: 12px;
}

/* 输入类控件 */
QLineEdit, QComboBox, QTextEdit {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 8px 10px;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
    border: 1px solid #3b82f6;
}

/* 列表 */
QListWidget#unitList {
    border: 1px solid #e5e7eb; border-radius: 10px; background: #ffffff;
}
QListWidget#unitList::item {
    padding: 8px 10px; border-bottom: 1px solid #f3f4f6;
}
QListWidget#unitList::item:selected {
    background: #eff6ff; color: #1f2937;
}

/* 表格 */
QTableWidget {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    gridline-color: #eef2f7;
    selection-background-color: #eff6ff;
    selection-color: #111827;
}
QHeaderView::section {
    background: #f9fafb; color: #374151; padding: 8px; border: none; border-bottom: 1px solid #e5e7eb;
}
QTableCornerButton::section {
    background: #f9fafb; border: none; border-bottom: 1px solid #e5e7eb;
}

/* 分割条 */
QSplitter::handle {
    background: #e5e7eb;
}
QSplitter::handle:hover {
    background: #c7d2fe;
}

/* 按钮主次 */
QPushButton#primary {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #3b82f6, stop:1 #60a5fa);
    color: white; border: none; border-radius: 10px; padding: 10px 16px; font-weight: 700;
}
QPushButton#primary:hover { filter: brightness(1.03); }
QPushButton#primary:disabled { background: #cfe1ff; color:#f3f4f6; }

QPushButton#accent {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #10b981, stop:1 #34d399);
    color: white; border: none; border-radius: 10px; padding: 10px 16px; font-weight: 700;
}
QPushButton#accent:disabled { background: #a7f3d0; color:#f3f4f6; }

QPushButton#secondary {
    background: #ffffff; color: #111827;
    border: 1px solid #e5e7eb; border-radius: 10px; padding: 8px 12px; font-weight: 600;
}
QPushButton#secondary:hover { background: #f9fafb; }

QPushButton#mini {
    background: #f3f4f6; color: #111827; border: none; border-radius: 8px; padding: 6px 8px; font-weight: 600;
}
QPushButton#mini:hover { background: #e5e7eb; }
QPushButton#miniDanger {
    background: #fee2e2; color: #b91c1c; border: none; border-radius: 8px; padding: 6px 8px; font-weight: 700;
}
QPushButton#miniDanger:hover { background: #fecaca; }

/* 学习页专用 */
QLabel#bigterm {
    font-size: 48px; font-weight: 900; color:#111827;
}
QPushButton#again { background: #fee2e2; color:#b91c1c; border-radius:10px; padding:10px 12px; font-weight:700; }
QPushButton#hard { background: #fef3c7; color:#92400e; border-radius:10px; padding:10px 12px; font-weight:700; }
QPushButton#good { background: #dcfce7; color:#065f46; border-radius:10px; padding:10px 12px; font-weight:700; }
QPushButton#easy { background: #dbeafe; color:#1e40af; border-radius:10px; padding:10px 12px; font-weight:700; }
"""

def apply_shadow(widget, radius=18, color=QtGui.QColor(17,17,17,40), offset=(0,6)):
    effect = QtWidgets.QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(radius)
    effect.setColor(color)
    effect.setOffset(*offset)
    widget.setGraphicsEffect(effect)

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = init_db()
        self.setWindowTitle("LANSGANBS")
        self.resize(1180, 760)
        self.setMinimumSize(980, 620)
        QtWidgets.QApplication.instance().setStyleSheet(APP_STYLE)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 顶部标题栏（不再包含导入/导出/主题切换）
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("多邻国日语词汇录入")
        title.setObjectName("appTitle")
        header.addWidget(title)
        header.addStretch()
        root.addLayout(header)

        # 分栏：左（单元）- 中（添加与表格）- 右（复习）
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # 左：单元列表
        left_frame = QtWidgets.QFrame()
        left_frame.setObjectName("panel")
        apply_shadow(left_frame)
        left_layout = QtWidgets.QVBoxLayout(left_frame)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(10)

        lbl = QtWidgets.QLabel("单元列表")
        lbl.setObjectName("sectionTitle")
        left_layout.addWidget(lbl)

        self.unit_list = QtWidgets.QListWidget()
        self.unit_list.setObjectName("unitList")
        self.unit_list.itemClicked.connect(self.on_unit_clicked)
        left_layout.addWidget(self.unit_list, 1)

        btns_left = QtWidgets.QHBoxLayout()
        btn_new_unit = QtWidgets.QPushButton("新建单元")
        btn_new_unit.setObjectName("secondary")
        btn_new_unit.clicked.connect(self.create_unit_dialog)
        btn_refresh_units = QtWidgets.QPushButton("刷新单元")
        btn_refresh_units.setObjectName("secondary")
        btn_refresh_units.clicked.connect(self.refresh_units)
        btns_left.addWidget(btn_new_unit)
        btns_left.addWidget(btn_refresh_units)
        btn_export_csv = QtWidgets.QPushButton("导出 CSV")
        btn_export_csv.setObjectName("secondary")
        btn_export_csv.clicked.connect(self.export_csv_dialog)
        btns_left.addWidget(btn_export_csv)
        left_layout.addLayout(btns_left)

        splitter.addWidget(left_frame)
        splitter.setStretchFactor(0, 0)

        # 中：添加 + 单元词表
        center_frame = QtWidgets.QFrame()
        center_frame.setObjectName("panel")
        apply_shadow(center_frame)
        center_layout = QtWidgets.QVBoxLayout(center_frame)
        center_layout.setContentsMargins(16, 16, 16, 16)
        center_layout.setSpacing(12)

        # 区块标题
        add_title = QtWidgets.QLabel("添加到题库")
        add_title.setObjectName("sectionTitle")
        center_layout.addWidget(add_title)

        # --- 1) MainWindow.__init__ 内，替换“添加区域”这段（从 add_row/form_col 到按钮列之前），先删除孤立的 self.add_mean 行 ---
        add_row = QtWidgets.QHBoxLayout()
        add_row.setSpacing(12)

        form_col = QtWidgets.QFormLayout()
        form_col.setLabelAlignment(QtCore.Qt.AlignRight)

        # 语言
        self.add_language = QtWidgets.QComboBox()
        self.add_language.setEditable(True)
        self.add_language.addItems(["日语", "韩语", "其他"])
        form_col.addRow("语言", self.add_language)

        # 单元
        self.add_unit_combo = QtWidgets.QComboBox()
        self.add_unit_combo.setEditable(True)
        self.refresh_units_to_combo()
        form_col.addRow("单元", self.add_unit_combo)

        # 唯一一套输入框
        self.add_term = QtWidgets.QLineEdit()
        form_col.addRow("日语词条", self.add_term)

        self.add_kanji = QtWidgets.QLineEdit()
        form_col.addRow("汉字写法(可选)", self.add_kanji)

        self.add_kana = QtWidgets.QLineEdit()
        form_col.addRow("罗马音(可选)", self.add_kana)

        # 语言切换：仅“日语”显示汉字/罗马音
        def on_language_changed():
            lang = (self.add_language.currentText() or "").strip()
            is_jp = (lang == "日语")
            kanji_label = form_col.labelForField(self.add_kanji)
            kana_label = form_col.labelForField(self.add_kana)
            for w in (self.add_kanji, self.add_kana, kanji_label, kana_label):
                if w:
                    w.setVisible(is_jp)

        self.add_language.currentTextChanged.connect(lambda _: on_language_changed())
        on_language_changed()

        # 中文释义（只创建一次）
        self.add_mean = QtWidgets.QLineEdit()
        form_col.addRow("中文释义", self.add_mean)

        # 自动填充标志（各绑定一次）
        self._term_autofilled = True
        self._kanji_autofilled = True
        self._kana_autofilled = True
        self._meaning_autofilled = True
        self.add_term.textEdited.connect(lambda _=None: setattr(self, "_term_autofilled", False))
        self.add_kanji.textEdited.connect(lambda _=None: setattr(self, "_kanji_autofilled", False))
        self.add_kana.textEdited.connect(lambda _=None: setattr(self, "_kana_autofilled", False))
        self.add_mean.textEdited.connect(lambda _=None: setattr(self, "_meaning_autofilled", False))

        # 假名→罗马音自动填充（已有实现则保持）
        self.add_term.textChanged.connect(self._auto_fill_kana_from_term)

        # 释义自动填充定时器（去抖 300ms）
        self._mean_timer = QtCore.QTimer(self)
        self._mean_timer.setSingleShot(True)
        self._mean_timer.timeout.connect(self._auto_fill_meaning_from_term)
        self.add_term.textChanged.connect(self._restart_mean_timer)
        self.add_kanji.textChanged.connect(self._restart_mean_timer)
        self.add_kana.textChanged.connect(self._restart_mean_timer)

        add_row.addLayout(form_col, 1)

        btncol = QtWidgets.QVBoxLayout()
        self.btn_add = QtWidgets.QPushButton("添加到题库")
        self.btn_add.setObjectName("secondary")
        self.btn_add.clicked.connect(self.add_card_from_form)
        btncol.addWidget(self.btn_add)
        btncol.addStretch()
        add_row.addLayout(btncol)

        center_layout.addLayout(add_row)

        # 分隔提示
        hint = QtWidgets.QLabel("请选择左侧单元查看该单元的词条，或选择“所有单元”")
        hint.setObjectName("muted")
        self.center_hint = hint
        center_layout.addWidget(self.center_hint)

        # 表格
        self.unit_table = QtWidgets.QTableWidget()
        self.unit_table.setColumnCount(5)
        self.unit_table.setHorizontalHeaderLabels(["ID", "日语词条", "罗马音", "释义", "操作"])
        header = self.unit_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)  # ID
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)  # 日语词条（合并列）
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)  # 假名读音
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)  # 释义
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)  # 操作
        self.unit_table.setAlternatingRowColors(True)
        self.unit_table.verticalHeader().setVisible(False)
        self.unit_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.unit_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.unit_table.hide()
        center_layout.addWidget(self.unit_table, 1)

        splitter.addWidget(center_frame)
        splitter.setStretchFactor(1, 1)

        # 右：学习/复习
        right_frame = QtWidgets.QFrame()
        right_frame.setObjectName("panel")
        apply_shadow(right_frame)
        right_layout = QtWidgets.QVBoxLayout(right_frame)
        right_layout.setContentsMargins(14, 16, 14, 16)
        right_layout.setSpacing(10)

        right_title = QtWidgets.QLabel("学习 / 复习")
        right_title.setObjectName("sectionTitle")
        right_layout.addWidget(right_title)

        self.study_unit_select = QtWidgets.QComboBox()
        self.study_unit_select.addItem("所有单元")
        self.refresh_study_units()
        right_layout.addWidget(self.study_unit_select)

        self.btn_go_study = QtWidgets.QPushButton("开始复习")
        self.btn_go_study.setObjectName("accent")
        self.btn_go_study.clicked.connect(self.open_study_window)
        self.btn_go_study.setFixedHeight(44)
        right_layout.addWidget(self.btn_go_study)

        right_layout.addStretch()
        splitter.addWidget(right_frame)
        splitter.setStretchFactor(2, 0)

        # UI 初始化
        self.refresh_units()

        btn_new_unit.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogNewFolder))
        btn_refresh_units.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_BrowserReload))
        self.btn_add.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogApplyButton))
        self.btn_go_study.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay))

        # 键盘快捷（space 作继续）
        self.shortcut_space = QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self)
        self.shortcut_space.activated.connect(self.on_space_pressed)

    # ---------- 单元管理 ----------
    def refresh_units(self):
        self.unit_list.clear()
        self.unit_list.addItem("所有单元")
        for u in list_units(self.db):
            self.unit_list.addItem(u)
        self.refresh_units_to_combo()
        self.refresh_study_units()
        self.unit_table.hide()
        self.center_hint.setText("请选择左侧单元查看该单元的词条，或选择“所有单元”")

    def refresh_units_to_combo(self):
        units = list_units(self.db)
        cur = self.add_unit_combo.currentText() if hasattr(self, 'add_unit_combo') else ""
        self.add_unit_combo.clear()
        self.add_unit_combo.addItem("")
        for u in units:
            self.add_unit_combo.addItem(u)
        idx = self.add_unit_combo.findText(cur)
        if idx >= 0:
            self.add_unit_combo.setCurrentIndex(idx)
        else:
            if cur:
                self.add_unit_combo.setEditText(cur)

    def refresh_study_units(self):
        units = list_units(self.db)
        self.study_unit_select.clear()
        self.study_unit_select.addItem("所有单元")
        for u in units:
            self.study_unit_select.addItem(u)

    def create_unit_dialog(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "新建单元", "单元名：")
        if ok and text.strip():
            self.add_unit_combo.setEditText(text.strip())
            QtWidgets.QMessageBox.information(self, "已设置", "已填入单元名，请继续添加词条。")

    # 4) 自动补读音（罗马音）与中文释义（替换 MainWindow 内这两个方法）
    def _impl_auto_fill_kana_from_term(self):
        lang = (self.add_language.currentText() or "").strip()
        if lang != "日语":
            return
        # 用户一旦手动编辑过罗马音，就不再自动覆盖
        if not getattr(self, "_kana_autofilled", True):
            return
        term = (self.add_term.text() or "").strip()
        if not term:
            return
        # 仅当词条是纯假名时，自动生成罗马音
        if _has_kana(term) and not _has_kanji(term):
            romaji = kana_to_romaji(term)
            if romaji:
                self.add_kana.setText(romaji)

    def _auto_fill_kana_from_term(self):
        lang = (self.add_language.currentText() or "").strip()
        if lang != "日语":
            return
        # 用户一旦手动编辑过罗马音，就不再自动覆盖
        if not getattr(self, "_kana_autofilled", True):
            return
        term = (self.add_term.text() or "").strip()
        if not term:
            return
        # 仅当词条是纯假名时，自动生成罗马音
        if _has_kana(term) and not _has_kanji(term):
            romaji = kana_to_romaji(term)
            if romaji:
                self.add_kana.setText(romaji)

    def _restart_mean_timer(self):
        # 统一的防抖触发
        if hasattr(self, "_mean_timer") and self._mean_timer is not None:
            self._mean_timer.stop()
            self._mean_timer.start(300)

    # --- 2) 在 MainWindow 内替换 _auto_fill_meaning_from_term，并新增 _is_japanese_like ---
    # 放到 class MainWindow 中（__init__ 外部），保持现有 self._mean_timer 绑定不变
    def _is_japanese_like(self, s: str) -> bool:
        if not s:
            return False
        for ch in s:
            code = ord(ch)
            if (0x3040 <= code <= 0x30FF) or (0x31F0 <= code <= 0x31FF) or (0x4E00 <= code <= 0x9FFF):
                return True
        return False

    def _auto_fill_meaning_from_term(self, force: bool = False):
        # 仅在“日语”下生效
        lang = (self.add_language.currentText() or "").strip()
        if "日" not in lang:
            return
        # 用户已手写且非强制，不覆盖
        if not force and (self.add_mean.text() or "").strip():
            return

        term = (self.add_term.text() or "").strip()
        kanji = (self.add_kanji.text() or "").strip()

        # 候选键：汉字 > 词条；只保留“像日文”的键
        raw_keys = []
        if kanji: raw_keys.append(kanji)
        if term and term not in raw_keys: raw_keys.append(term)
        keys = [k for k in raw_keys if self._is_japanese_like(k)]
        if not keys:
            return

        hit = None
        hit_key = ""
        for k in keys:
            e = suggest_full_entry(k)  # (e_term, e_kana, e_mean)
            if e:
                hit = e
                hit_key = k
                break
        if not hit:
            print(f"[AutoFill] 未命中: {keys}")
            return

        e_term, e_kana, e_mean = hit
        filled = []

        # 填“汉字写法”：当命中项 term 含汉字时
        if e_term and any(0x4E00 <= ord(c) <= 0x9FFF for c in e_term):
            cur = (self.add_kanji.text() or "").strip()
            if force or not cur or getattr(self, "_kanji_autofilled", True):
                if not cur or cur != e_term:
                    self.add_kanji.setText(e_term)
                    self._kanji_autofilled = True
                    filled.append(f"kanji={e_term}")

        # 填“日语词条”：用命中项 kana（假名）优先
        if e_kana:
            cur = (self.add_term.text() or "").strip()
            if force or not cur or getattr(self, "_term_autofilled", True):
                if not cur or cur != e_kana:
                    self.add_term.setText(e_kana)
                    self._term_autofilled = True
                    filled.append(f"term={e_kana}")

        # 填“中文释义”
        if e_mean:
            cur = (self.add_mean.text() or "").strip()
            if force or not cur or getattr(self, "_meaning_autofilled", True):
                if not cur or cur != e_mean:
                    self.add_mean.setText(e_mean)
                    self._meaning_autofilled = True
                    filled.append("meaning")

        if filled:
            print(f"[AutoFill] 命中: {hit_key} -> {', '.join(filled)}")
        else:
            print(f"[AutoFill] 命中但未覆盖（均视为手动输入）：{hit_key}")

    # ---------- 选择单元查看词条 ----------
    def on_unit_clicked(self, item):
        unit = item.text()
        if unit == "所有单元":
            rows = list_cards_by_unit(self.db, None)
        else:
            rows = list_cards_by_unit(self.db, unit)
        self.populate_unit_table(rows)
        self.unit_table.show()
        self.center_hint.setText(f"当前单元：{unit}（共 {len(rows)} 条）")

    def populate_unit_table(self, rows):
        # 先设行数，避免“显示条数不对”
        self.unit_table.clearContents()
        self.unit_table.setRowCount(len(rows))

        for row_idx, r in enumerate(rows):
            # ID
            id_item = QtWidgets.QTableWidgetItem(str(r[0]))
            id_item.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
            id_item.setTextAlignment(QtCore.Qt.AlignCenter)

            # 日语词条（假名 | 汉字）
            term_text = format_jp_term_for_table(r)
            term_item = QtWidgets.QTableWidgetItem(term_text)

            # 假名读音
            kana_item = QtWidgets.QTableWidgetItem(get_jp_kana(r))

            # 释义
            mean_item = QtWidgets.QTableWidgetItem(r[4] or "")

            # 写入单元格
            self.unit_table.setItem(row_idx, 0, id_item)
            self.unit_table.setItem(row_idx, 1, term_item)
            self.unit_table.setItem(row_idx, 2, kana_item)
            self.unit_table.setItem(row_idx, 3, mean_item)

            # 操作列：图标按钮（编辑/删除）
            action_w = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(action_w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)

            btn_edit = QtWidgets.QToolButton()
            btn_edit.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogDetailedView))
            btn_edit.setToolTip("编辑")
            btn_edit.setCursor(QtCore.Qt.PointingHandCursor)
            btn_edit.setProperty("card_id", r[0])
            btn_edit.clicked.connect(self.edit_card_dialog)

            btn_delete = QtWidgets.QToolButton()
            btn_delete.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TrashIcon))
            btn_delete.setToolTip("删除")
            btn_delete.setCursor(QtCore.Qt.PointingHandCursor)
            btn_delete.setProperty("card_id", r[0])
            btn_delete.clicked.connect(self.delete_card_dialog)

            h.addWidget(btn_edit)
            h.addWidget(btn_delete)
            h.addStretch()
            self.unit_table.setCellWidget(row_idx, 4, action_w)

        # 若需要，显示表格
        if len(rows) > 0:
            self.unit_table.show()

    # ---------- 添加卡片 ----------
    def add_card_from_form(self):
        # 1) 读取表单
        language = (self.add_language.currentText() or "").strip() or "日语"
        unit = (self.add_unit_combo.currentText() or "").strip()
        term = (self.add_term.text() or "").strip()
        meaning = (self.add_mean.text() or "").strip()
        jp_kanji = (self.add_kanji.text() or "").strip() or None
        jp_kana = (self.add_kana.text() or "").strip() or None

        # 2) 兜底：日语时，若词条为纯假名且读音未填，则自动带入平假名
        if language == "日语":
            if (not jp_kana) and _has_kana(term) and not _has_kanji(term):
                try:
                    jp_kana = kana_to_romaji(term)  # 原：_kata_to_hira(term)
                except Exception:
                    pass

        # 3) 校验与补全显示字段
        if language == "日语":
            if not (term or jp_kanji or jp_kana):
                QtWidgets.QMessageBox.warning(self, "缺少内容", "请至少填写 日语词条／汉字写法／假名读音 之一。")
                return
            # 若 term 为空，用更合适的显示字段兜底（保证 NOT NULL）
            if not term:
                term = (jp_kanji or jp_kana or "").strip()
            add_card(self.db, language, unit, term, meaning, jp_kanji=jp_kanji, jp_kana=jp_kana)
        else:
            if not term:
                QtWidgets.QMessageBox.warning(self, "缺少内容", "请填写词条。")
                return
            add_card(self.db, language, unit, term, meaning)

        # 4) 清空与刷新
        self.add_term.clear()
        self.add_mean.clear()
        self.add_kanji.clear()
        self.add_kana.clear()
        self._kana_autofilled = True
        self.refresh_units()
        QtWidgets.QMessageBox.information(self, "已添加", "已将该单词加入题库。")

    def add_and_start_unit(self):
        self.add_card_from_form()
        unit = self.add_unit_combo.currentText().strip()
        # 跳转复习窗口（保留原有功能）
        self.open_study_window(unit_filter=(unit if unit else None))

    # ---------- 编辑 / 删除 ----------
    def delete_card_dialog(self):
        btn = self.sender()
        cid = btn.property("card_id")
        reply = QtWidgets.QMessageBox.question(self, "确认删除", "确定删除该词？", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if reply == QtWidgets.QMessageBox.Yes:
            delete_card(self.db, cid)
            self.refresh_units()

    def edit_card_dialog(self):
        btn = self.sender()
        card_id = btn.property("card_id")
        row = get_card_by_id(self.db, int(card_id))
        if not row:
            QtWidgets.QMessageBox.warning(self, "未找到", "未找到该词条。")
            return
        dlg = EditDialog(self, self.db, row)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            data = dlg.get_result()
            update_card_fields_full(
                self.db, row[0],
                data["language"], data["unit"], data["term"], data["meaning"],
                data["jp_kanji"], data["jp_kana"], None
            )
            # 刷新当前列表，不弹“已保存”
            current = self.unit_list.currentItem()
            unit = None if not current or current.text() == "所有单元" else current.text()
            rows = list_cards_by_unit(self.db, unit)
            self.populate_unit_table(rows)

    # ---------- 学习窗口 ----------
    def open_study_window(self, unit_filter=None):
        if unit_filter is None:
            selected = self.study_unit_select.currentText()
            unit_filter = None if selected == "所有单元" else selected
        self.study_win = StudyWindow(self.db, unit_filter)
        self.study_win.show()

    # ---------- 快捷键（Space 用作继续） ----------
    def on_space_pressed(self):
        if hasattr(self, 'study_win') and self.study_win.isVisible():
            # delegate
            self.study_win.try_space_continue()

    # 2) 在 class MainWindow 内新增该方法（与按钮 self.export_csv_dialog 绑定一致）
    def export_csv_dialog(self):
        # 默认导出到桌面，带时间戳文件名
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_dir = os.path.join(os.path.expanduser("~"), "Desktop")
        default_path = os.path.join(default_dir, f"vocab_export_{ts}.csv")

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出为 CSV",
            default_path,
            "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            export_all_cards_to_csv(self.db, path)
            QtWidgets.QMessageBox.information(self, "导出完成", f"已导出到：{path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导出失败", f"错误：{e}")

class EditDialog(QtWidgets.QDialog):
    def __init__(self, parent, conn, row):
        super().__init__(parent)
        self.setWindowTitle("编辑词条")
        self.setModal(True)
        self.conn = conn
        self.row = row  # 完整行
        self.resize(420, 0)

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)

        # 语言
        self.cb_lang = QtWidgets.QComboBox()
        self.cb_lang.setEditable(True)
        # 预置一些常用项
        self.cb_lang.addItems(["日语", "韩语", "英语", "其他"])
        # 当前值
        cur_lang = (row[1] or "").strip()
        if self.cb_lang.findText(cur_lang) < 0:
            self.cb_lang.addItem(cur_lang)
        self.cb_lang.setCurrentText(cur_lang)
        form.addRow("语言", self.cb_lang)

        # 单元（下拉选择，不手动输入）
        self.cb_unit = QtWidgets.QComboBox()
        self.cb_unit.setEditable(False)
        units = list_units(conn)
        self.cb_unit.addItem("")  # 允许空单元
        for u in units:
            self.cb_unit.addItem(u)
        cur_unit = (row[2] or "").strip()
        # 若当前单元不在列表，追加后选中
        if self.cb_unit.findText(cur_unit) < 0:
            self.cb_unit.addItem(cur_unit)
        self.cb_unit.setCurrentText(cur_unit)

        # 新建单元按钮
        unit_row = QtWidgets.QHBoxLayout()
        unit_row.addWidget(self.cb_unit, 1)
        btn_new_unit = QtWidgets.QToolButton()
        btn_new_unit.setText("新建")
        btn_new_unit.clicked.connect(self._create_unit)
        unit_row.addWidget(btn_new_unit)
        unit_w = QtWidgets.QWidget()
        unit_w.setLayout(unit_row)
        form.addRow("单元", unit_w)

        # 日语词条（原文）
        self.ed_term = QtWidgets.QLineEdit(row[3] or "")
        form.addRow("日语词条", self.ed_term)

        # 汉字写法 / 假名读音
        jp_kanji = row[11] if len(row) > 11 else ""
        jp_kana  = row[12] if len(row) > 12 else ""
        self.ed_kanji = QtWidgets.QLineEdit(jp_kanji or "")
        self.ed_kana  = QtWidgets.QLineEdit(jp_kana or "")
        form.addRow("汉字写法", self.ed_kanji)
        form.addRow("假名读音", self.ed_kana)

        # 中文释义
        self.ed_mean = QtWidgets.QLineEdit(row[4] or "")
        form.addRow("中文释义", self.ed_mean)

        layout.addLayout(form)

        # 按钮区
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _create_unit(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "新建单元", "单元名：")
        name = (text or "").strip()
        if ok and name:
            if self.cb_unit.findText(name) < 0:
                self.cb_unit.addItem(name)
            self.cb_unit.setCurrentText(name)

    def _on_save(self):
        # 简单校验：日语词条或（汉字/假名）至少一项
        term = (self.ed_term.text() or "").strip()
        kanji = (self.ed_kanji.text() or "").strip()
        kana  = (self.ed_kana.text() or "").strip()
        if not term and not kanji and not kana:
            QtWidgets.QMessageBox.warning(self, "校验失败", "请至少填写 日语词条 或 汉字/假名 之一。")
            return
        self.accept()

    def get_result(self):
        return {
            "language": (self.cb_lang.currentText() or "").strip(),
            "unit": (self.cb_unit.currentText() or "").strip(),
            "term": (self.ed_term.text() or "").strip(),
            "meaning": (self.ed_mean.text() or "").strip(),
            "jp_kanji": (self.ed_kanji.text() or "").strip() or None,
            "jp_kana": (self.ed_kana.text() or "").strip() or None,
        }


class StudyWindow(QtWidgets.QWidget):
    def __init__(self, db, unit_filter=None):
        super().__init__()
        self.db = db
        self.unit_filter = unit_filter
        self.setWindowTitle(f"复习 - {'全部单元' if not unit_filter else unit_filter}")
        self.resize(820, 560)
        QtWidgets.QApplication.instance().setStyleSheet(APP_STYLE)

        # 顶部信息栏
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        top = QtWidgets.QHBoxLayout()
        self.info_label = QtWidgets.QLabel("准备复习...")
        self.info_label.setObjectName("sectionTitle")
        top.addWidget(self.info_label)
        top.addStretch()
        v.addLayout(top)

        # 卡片展示
        card = QtWidgets.QFrame()
        card.setObjectName("panel")
        apply_shadow(card, radius=16, offset=(0,5))
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(18, 24, 18, 18)
        card_layout.setSpacing(10)
        self.card_layout = card_layout  # 存储以便后续插入控件

        self.term_label = QtWidgets.QLabel("——")
        self.term_label.setObjectName("bigterm")
        self.term_label.setAlignment(QtCore.Qt.AlignCenter)
        card_layout.addWidget(self.term_label)

        self.mean_label = QtWidgets.QLabel("")
        self.mean_label.setAlignment(QtCore.Qt.AlignCenter)
        self.mean_label.setWordWrap(True)
        self.mean_label.setStyleSheet("color:#4b5563;")  # muted
        self.mean_label.hide()
        card_layout.addWidget(self.mean_label)

        self.btn_toggle = QtWidgets.QPushButton("显示 / 隐藏 释义")
        self.btn_toggle.setObjectName("secondary")
        self.btn_toggle.clicked.connect(self.toggle_meaning)
        self.btn_toggle.setFixedWidth(160)
        card_layout.addWidget(self.btn_toggle, 0, QtCore.Qt.AlignHCenter)

        # 输入与确认（仅中→日用，懒创建）
        self.answer_box = None
        self.input_answer = None
        self.btn_confirm = None

        # 评分按钮（仅日→中显示）
        rating = QtWidgets.QHBoxLayout()
        rating.setSpacing(10)
        self.btn_again = QtWidgets.QPushButton("再来一次")
        self.btn_again.setObjectName("again")
        self.btn_hard = QtWidgets.QPushButton("困难")
        self.btn_hard.setObjectName("hard")
        self.btn_good = QtWidgets.QPushButton("记住了")
        self.btn_good.setObjectName("good")
        self.btn_easy = QtWidgets.QPushButton("非常容易")
        self.btn_easy.setObjectName("easy")
        for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
            b.setEnabled(False)
            rating.addWidget(b)

        self.btn_again.clicked.connect(lambda _=False: self.submit_rating(1))
        self.btn_hard.clicked.connect(lambda _=False: self.submit_rating(3))
        self.btn_good.clicked.connect(lambda _=False: self.submit_rating(4))
        self.btn_easy.clicked.connect(lambda _=False: self.submit_rating(5))

        # “没记住”按钮（仅中→日显示）
        self.btn_fail = QtWidgets.QPushButton("没记住")
        self.btn_fail.setObjectName("again")
        self.btn_fail.setEnabled(False)
        self.btn_fail.clicked.connect(self.mark_fail)
        rating.addWidget(self.btn_fail)

        card_layout.addLayout(rating)
        v.addWidget(card, 1)

        # 显著的继续按钮
        bottom = QtWidgets.QHBoxLayout()
        self.continue_btn = QtWidgets.QPushButton("继续（Space）")
        self.continue_btn.setObjectName("primary")
        self.continue_btn.setFixedHeight(50)
        self.continue_btn.setEnabled(False)
        self.continue_btn.clicked.connect(self.next_card)
        bottom.addStretch()
        bottom.addWidget(self.continue_btn, 0)
        bottom.addStretch()
        v.addLayout(bottom)

        # 内部状态
        self.queue = []
        self.idx = 0
        self.current = None
        self.session_total = 0
        self.session_correct = 0
        self.showing_mean = False

        # 准备复习队列
        self.prepare_queue()
        if not self.queue:
            QtWidgets.QMessageBox.information(
                self, "信息",
                "当前没有到期的单词可复习（或刚录入的单元未到首次复习时间）。"
            )
            self.close()
            return
        self.show_card(self.queue[self.idx])

    # 文件：`vocab_v2.1.0.py`，类 StudyWindow 内
    def submit_rating(self, quality, _checked=False):
        if not self.current:
            return
        row, mode = self.current
        if mode != 0:
            return
        interval, repetition, ef = sm2_update(row, quality)
        last_review = datetime.utcnow().isoformat()
        update_card_review(self.db, row[0], interval, repetition, ef, last_review, None)

        for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
            b.setEnabled(False)

        self.session_total += 1
        if quality >= 4:
            self.session_correct += 1

        # 评分后显示中文释义
        zh = (row[4] or "").strip()
        self.mean_label.setText(zh)
        self.mean_label.show()
        self.showing_mean = True

        self.continue_btn.setEnabled(True)
        self.info_label.setText(f"已做 {self.session_total}，正确 {self.session_correct}")

    def prepare_queue(self):
        rows = list_due_cards(self.db, unit=self.unit_filter)
        self.queue = []
        n = len(rows)
        if n == 0:
            return
        num_mode0 = int(n * 0.7)  # 日→中
        num_mode1 = n - num_mode0  # 中→日
        modes = [0] * num_mode0 + [1] * num_mode1
        random.shuffle(modes)
        for r, m in zip(rows, modes):
            self.queue.append((r, m))

    def _ensure_answer_box(self):
        if self.answer_box:
            return
        self.answer_box = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(self.answer_box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.input_answer = QtWidgets.QLineEdit()
        self.input_answer.setPlaceholderText("输入假名或汉字")
        self.btn_confirm = QtWidgets.QPushButton("确认")
        self.btn_confirm.setObjectName("secondary")
        self.btn_confirm.clicked.connect(self.check_answer)
        h.addWidget(self.input_answer, 1)
        h.addWidget(self.btn_confirm, 0)
        # 插入到“显示/隐藏 释义”按钮之后
        idx = self.card_layout.indexOf(self.btn_toggle)
        self.card_layout.insertWidget(idx + 1, self.answer_box)

    # StudyWindow 内，替换以下三个方法
    def show_card(self, card):
        row, mode = card
        self.current = (row, mode)
        self.showing_mean = False

        # 清理状态
        self.mean_label.clear()
        self.mean_label.hide()
        self.continue_btn.setEnabled(False)

        # 控制评分按钮可见性
        for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
            b.setVisible(mode == 0)
            b.setEnabled(mode == 0)
        self.btn_fail.setVisible(mode == 1)
        self.btn_fail.setEnabled(mode == 1)

        # 输入区域
        if mode == 1:
            self._ensure_answer_box()
            self.answer_box.show()
            self.input_answer.clear()
            self.input_answer.setEnabled(True)
            self.btn_confirm.setEnabled(True)
            self.input_answer.setFocus()
        else:
            if self.answer_box:
                self.answer_box.hide()

        # 展示题面
        if mode == 0:
            kana = pick_kana(row)
            kanji = get_jp_kanji(row)
            romaji = pick_romaji(row, kana)
            big_css = "font-size:46px; font-weight:800; line-height:1.2;"
            small_css = "font-size:24px; color:#6b7280; line-height:1.2;"
            parts = []
            if romaji:
                parts.append(f"<div style='{small_css}'>{html.escape(romaji)}</div>")
            if kana:
                parts.append(f"<div style='{big_css}'>{html.escape(kana)}</div>")
            if kanji:
                parts.append(f"<div style='{small_css}'>{html.escape(kanji)}</div>")
            if not parts:
                term = (row[3] or "").strip()
                parts.append(f"<div style='{big_css}'>{html.escape(term) if term else '——'}</div>")
            self.term_label.setText("<div style='text-align:center'>" + "".join(parts) + "</div>")
            self.term_label.show()
        else:
            zh = (row[4] or "").strip()
            self.term_label.setText(
                f"<div style='text-align:center; font-size:28px; font-weight:800'>{html.escape(zh) if zh else '——'}</div>")
            self.term_label.show()

    def _show_jp_answer_block(self, row, correct: bool):
        # 统一答案展示块（含对错提示）
        kana = pick_kana(row)
        kanji = get_jp_kanji(row)
        if not kana and not kanji:
            # 回退 term
            term = (row[3] or "").strip()
            kana = term if term else ""
        romaji = pick_romaji(row, kana)
        big_css = "font-size:28px; font-weight:800; line-height:1.2;"
        small_css = "font-size:16px; color:#6b7280; line-height:1.2;"
        state_css = "color:#059669;font-weight:800;" if correct else "color:#b91c1c;font-weight:800;"
        state_txt = "✔ 正确" if correct else "✘ 错误"
        answer_parts = [f"<div style='{state_css}'>{state_txt}</div>"]
        if romaji:
            answer_parts.append(f"<div style='{small_css}'>{html.escape(romaji)}</div>")
        if kana:
            answer_parts.append(f"<div style='{big_css}'>{html.escape(kana)}</div>")
        if kanji:
            answer_parts.append(f"<div style='{small_css}'>{html.escape(kanji)}</div>")
        zh = (row[4] or "").strip()
        tip = "<div style='text-align:center'>" + "".join(answer_parts) + (f"<div style='{small_css}'>{html.escape(zh)}</div>" if zh else "") + "</div>"
        return tip

    # --- StudyWindow.check_answer：“中→日”只接受非空的假名或汉字 ---
    def check_answer(self):
        if not self.current:
            return
        row, mode = self.current
        if mode != 1:
            return

        user_input = norm(self.input_answer.text())
        answers = set()
        kana = pick_kana(row)
        kanji = get_jp_kanji(row)
        if kana:
            answers.add(norm(kana))
        if kanji:
            answers.add(norm(kanji))
        term = norm(row[3])
        if term and (_has_kana(term) or _has_kanji(term)):
            answers.add(term)

        correct = bool(user_input) and (norm(user_input) in answers)

        # 展示答案
        self.mean_label.setText(self._show_jp_answer_block(row, correct))
        self.mean_label.show()
        self.showing_mean = True

        # 更新记忆算法与计数
        q = 4 if correct else 1
        interval, repetition, ef = sm2_update(row, q)
        last_review = datetime.utcnow().isoformat()
        update_card_review(self.db, row[0], interval, repetition, ef, last_review, None)
        self.session_total += 1
        if correct:
            self.session_correct += 1
        self.info_label.setText(f"已做 {self.session_total}，正确 {self.session_correct}")

        # UI 状态
        self.continue_btn.setEnabled(True)
        if self.answer_box:
            self.input_answer.setEnabled(False)
            self.btn_confirm.setEnabled(False)
        self.btn_fail.setEnabled(False)

    def mark_fail(self):
        if not self.current:
            return
        row, mode = self.current
        if mode != 1:
            return

        # 无需输入，直接按错误处理并展示答案
        self.mean_label.setText(self._show_jp_answer_block(row, correct=False))
        self.mean_label.show()
        self.showing_mean = True

        # 更新记忆算法与计数（质量=1）
        interval, repetition, ef = sm2_update(row, 1)
        last_review = datetime.utcnow().isoformat()
        update_card_review(self.db, row[0], interval, repetition, ef, last_review, None)
        self.session_total += 1
        self.info_label.setText(f"已做 {self.session_total}，正确 {self.session_correct}")

        # UI 状态
        self.continue_btn.setEnabled(True)
        if self.answer_box:
            self.input_answer.setEnabled(False)
            self.btn_confirm.setEnabled(False)
        self.btn_fail.setEnabled(False)

    # --- StudyWindow.toggle_meaning：在“日→中”中切换释义并启用/禁用评分按钮 ---
    def toggle_meaning(self):
        if not self.current:
            return
        row, mode = self.current
        if self.showing_mean:
            # 隐藏答案
            self.mean_label.hide()
            self.showing_mean = False
            if mode == 0:
                # 日→中：未展开时不允许评分/继续
                for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                    b.setEnabled(False)
                self.continue_btn.setEnabled(False)
            return

        # 展开答案/释义
        if mode == 0:
            zh = (row[4] or "").strip()
            self.mean_label.setText(zh)
            # 展开后允许评分与继续
            for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                b.setEnabled(True)
            self.continue_btn.setEnabled(True)
        else:
            # 中→日：显示标准答案（不计分）
            self.mean_label.setText(self._show_jp_answer_block(row, correct=False))
            self.continue_btn.setEnabled(True)
            if self.answer_box:
                self.input_answer.setEnabled(False)
                self.btn_confirm.setEnabled(False)
            self.btn_fail.setEnabled(False)

        self.mean_label.show()
        self.showing_mean = True

    def next_card(self):
        self.idx += 1
        if self.idx >= len(self.queue):
            QtWidgets.QMessageBox.information(self, "完成", "本轮复习完成。")
            self.close()
            return
        self.show_card(self.queue[self.idx])

    def try_space_continue(self):
        if self.continue_btn.isEnabled():
            self.next_card()
        else:
            # 中→日时将焦点给输入框
            if self.current and self.current[1] == 1 and self.answer_box:
                self.input_answer.setFocus()

def main():
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()