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

# 1) DB 查询：支持单元/多单元/全部（统一不再按到期过滤）
def list_cards_by_unit(conn, unit=None):
    cur = conn.cursor()
    # 全部
    if unit is None or unit == "" or unit == "所有单元":
        cur.execute('SELECT * FROM cards ORDER BY unit, id')
        return cur.fetchall()
    # 多单元
    if isinstance(unit, (list, tuple, set)):
        units = []
        seen = set()
        for u in unit:
            name = (u or "").strip()
            if not name or name == "所有单元":
                continue
            if name in seen:
                continue
            seen.add(name)
            units.append(name)
        # 空列表直接回退到全部
        if not units:
            cur.execute('SELECT * FROM cards ORDER BY unit, id')
            return cur.fetchall()
        qs = ",".join("?" for _ in units)
        cur.execute(f'SELECT * FROM cards WHERE unit IN ({qs}) ORDER BY unit, id', units)
        return cur.fetchall()
    # 单一
    cur.execute('SELECT * FROM cards WHERE unit = ? ORDER BY id', (unit,))
    return cur.fetchall()

# 保留占位的兼容名，但实际不再按到期筛选
def list_due_cards(conn, unit=None):
    return list_cards_by_unit(conn, unit)

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
        "term": "假名",
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

# --- 填充工具函数（占位补全） ---
def _has_kana(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        code = ord(ch)
        if (0x3040 <= code <= 0x309F) or (0x30A0 <= code <= 0x30FF):
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
            out.append(chr(code - 0x60))  # カタカナ→ひらがな
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
        "ゔ":"vu","ゎ":"wa","ゕ":"ka","ゖ":"ka",
        "ー":"-",  # 长音符，后面单独处理
        "っ":"",   # 促音，靠后面首辅音加倍
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
            # 用上一个 token 的元音延长
            out.append(last_vowel if last_vowel else "")
            continue
        out.append(token)
        for c in reversed(token):
            if c in vowels:
                last_vowel = c
                break
    return "".join(out)

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
# 1) 紧跟在 APP_STYLE 定义之后追加：更具体的 QComboBox 覆盖样式
APP_STYLE += """
/* QComboBox 美化覆盖（后定义覆盖先前合并选择器） */
QComboBox {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    padding: 6px 36px 6px 12px; /* 右侧为箭头预留空间 */
    min-height: 34px;
}
QComboBox:hover {
    border-color: #c7d2fe;
    background: #ffffff;
}
QComboBox:focus {
    border: 1px solid #3b82f6;
}
QComboBox:disabled {
    color: #9ca3af;
    background: #f3f4f6;
}

/* 右侧下拉按钮区 */
QComboBox::drop-down {
    width: 34px;
    border-left: 1px solid #e5e7eb;
    border-top-right-radius: 10px;
    border-bottom-right-radius: 10px;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #fafafa, stop:1 #f3f4f6);
}
QComboBox::drop-down:hover {
    background: #eef2ff;
    border-left-color: #c7d2fe;
}

/* 下拉列表弹出视图（QListView） */
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    padding: 6px 0;
    outline: 0;
}
QComboBox QAbstractItemView::item {
    padding: 8px 12px;
    min-height: 28px;
}
QComboBox QAbstractItemView::item:selected {
    background: #eff6ff;
    color: #111827;
}
QComboBox QAbstractItemView::item:hover {
    background: #f3f4f6;
}
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

        # 先创建分割条并放入根布局
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
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
        self.unit_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        left_layout.addWidget(self.unit_list, 1)

        # 1) 左侧按钮：用 2×2 网格替换原来的 QHBoxLayout，并把“导出 CSV”改为“导出”
        btns_left = QtWidgets.QGridLayout()

        btn_new_unit = QtWidgets.QPushButton("新建单元")
        btn_new_unit.setObjectName("secondary")
        btn_new_unit.clicked.connect(self.create_unit_dialog)

        btn_refresh_units = QtWidgets.QPushButton("刷新单元")
        btn_refresh_units.setObjectName("secondary")
        btn_refresh_units.clicked.connect(self.refresh_units)

        self.btn_merge_study = QtWidgets.QPushButton("复习")
        self.btn_merge_study.setObjectName("secondary")
        self.btn_merge_study.clicked.connect(self.open_study_window)

        btn_export_csv = QtWidgets.QPushButton("导出")  # 原为“导出 CSV”
        btn_export_csv.setObjectName("secondary")
        btn_export_csv.clicked.connect(self.export_csv_dialog)

        # 统一尺寸策略，单元格内尽量铺满
        for b in (btn_new_unit, btn_refresh_units, self.btn_merge_study, btn_export_csv):
            b.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            b.setMinimumHeight(34)

        # 2×2 网格摆放
        btns_left.addWidget(btn_new_unit, 0, 0)
        btns_left.addWidget(btn_refresh_units, 0, 1)
        btns_left.addWidget(self.btn_merge_study, 1, 0)
        btns_left.addWidget(btn_export_csv, 1, 1)
        btns_left.setHorizontalSpacing(8)
        btns_left.setVerticalSpacing(8)

        left_layout.addLayout(btns_left)

        # 2) 分割条和左侧宽度：去掉最大宽度限制，让用户可拖拽放大
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(6)
        left_frame.setMinimumWidth(180)
        # 去掉这一行或改为更大上限：
        # left_frame.setMaximumWidth(280)
        # 若需要保留上限，可调宽一些，例如：
        # left_frame.setMaximumWidth(360)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        # 初始尺寸略放大一些，避免两行按钮仍被截断
        QtCore.QTimer.singleShot(0, lambda: splitter.setSizes([280, max(700, self.width() - 280)]))

        splitter.addWidget(left_frame)

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

        # python
        # 添加区
        add_row = QtWidgets.QHBoxLayout()
        add_row.setSpacing(12)

        form_col = QtWidgets.QFormLayout()
        form_col.setLabelAlignment(QtCore.Qt.AlignRight)

        # 单元（默认可为空，用户可手动输入；下拉不含空项）
        self.add_unit_combo = QtWidgets.QComboBox()
        self.add_unit_combo.setEditable(True)
        self.refresh_units_to_combo()
        form_col.addRow("单元", self.add_unit_combo)
        self._beautify_combo(self.add_unit_combo)

        # 日语字段
        self.add_term = QtWidgets.QLineEdit()
        form_col.addRow("假名", self.add_term)

        self.add_kanji = QtWidgets.QLineEdit()
        form_col.addRow("汉字写法(可选)", self.add_kanji)

        self.add_kana = QtWidgets.QLineEdit()
        form_col.addRow("罗马音(可选)", self.add_kana)

        # 中文释义
        self.add_mean = QtWidgets.QLineEdit()
        form_col.addRow("中文释义", self.add_mean)

        # 自动填充与去抖
        self._term_autofilled = True
        self._kanji_autofilled = True
        self._kana_autofilled = True
        self._meaning_autofilled = True
        self._auto_kana_in_progress = False
        self._last_auto_romaji = ""

        self.add_term.textEdited.connect(lambda _=None: setattr(self, "_term_autofilled", False))
        self.add_kanji.textEdited.connect(lambda _=None: setattr(self, "_kanji_autofilled", False))
        self.add_kana.textEdited.connect(self._on_add_kana_edited)
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

        # 右侧按钮列
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

        # 在 __init__ 的 center_layout 里，紧跟 self.center_hint 之后添加：
        tools_bar = QtWidgets.QHBoxLayout()
        tools_bar.setSpacing(8)

        # 总览面板（单元被选中时显示）
        self.overview = QtWidgets.QFrame()
        self.overview.setObjectName("panel")
        apply_shadow(self.overview)
        ov_l = QtWidgets.QHBoxLayout(self.overview)
        ov_l.setContentsMargins(12, 8, 12, 8);
        ov_l.setSpacing(16)

        self.ov_count = QtWidgets.QLabel("— 词")
        self.ov_review = QtWidgets.QLabel("已复习 — 条｜重复分布 —")
        self.ov_recent = QtWidgets.QLabel("最近添加 / 复习：— / —")
        for w in (self.ov_count, self.ov_review, self.ov_recent):
            w.setObjectName("muted")
            ov_l.addWidget(w)

        # 工具区：筛选 + 排序 + 打乱 + 导出

        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.addItems(["默认顺序", "添加时间", "上次复习", "重复次数", "易度EF"])
        tools_bar.addWidget(self.sort_combo)

        self.btn_shuffle = QtWidgets.QPushButton("打乱顺序")
        self.btn_shuffle.setObjectName("mini")
        tools_bar.addWidget(self.btn_shuffle)

        self.btn_overview = QtWidgets.QPushButton("总览")
        self.btn_overview.setObjectName("mini")
        tools_bar.addWidget(self.btn_overview)
        self.btn_overview.clicked.connect(self._open_unit_overview)

        tools_bar.addStretch()

        # 搜索框
        self.search_box = QtWidgets.QLineEdit()
        self.search_box.setPlaceholderText("搜索：词条 / 汉字 / 假名 / 释义")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(280)
        tools_bar.addWidget(self.search_box)

        center_layout.addWidget(self.overview)  # 总览条
        center_layout.addLayout(tools_bar)  # 工具条
        self.overview.hide()  # 默认隐藏

        # 表格
        self.unit_table = QtWidgets.QTableWidget()
        self.unit_table.setColumnCount(6)
        self.unit_table.setHorizontalHeaderLabels(["ID", "假名", "汉字写法", "罗马音", "释义", "操作"])
        header = self.unit_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)  # ID
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)  # 假名
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)  # 汉字写法
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)  # 罗马音
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)  # 释义
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)  # 操作

        self.unit_table.setAlternatingRowColors(True)
        self.unit_table.verticalHeader().setVisible(False)
        self.unit_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.unit_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.unit_table.hide()
        center_layout.addWidget(self.unit_table, 1)

        splitter.addWidget(center_frame)

        # UI 初始化
        self.refresh_units()
        btn_new_unit.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogNewFolder))
        btn_refresh_units.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_BrowserReload))
        self.btn_add.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogApplyButton))

        # 键盘快捷（space 作继续）
        self.shortcut_space = QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self)
        self.shortcut_space.activated.connect(self.on_space_pressed)

        # --- 在 MainWindow.__init__ 末尾附近（UI 初始化后）追加状态与信号 ---
        self._current_unit = None
        self._current_rows_all = []  # 原始（当前单元）
        self._current_rows_view = []  # 过滤/排序/打乱后的“当前显示”
        self._shuffled = False

        # 绑定信号
        self.search_box.textChanged.connect(self._apply_filters_and_refresh)
        self.sort_combo.currentIndexChanged.connect(self._apply_filters_and_refresh)
        self.btn_shuffle.clicked.connect(self._on_shuffle_clicked)

    # --- 统计 & 过滤 & 排序 ---
    def _calc_overview(self, rows):
        total = len(rows)
        cnt_kana = sum(1 for r in rows if (len(r) > 12 and (r[12] or "").strip()))
        cnt_kanji = sum(1 for r in rows if (len(r) > 11 and (r[11] or "").strip()))
        cnt_mean = sum(1 for r in rows if (r[4] or "").strip())
        cnt_reviewed = sum(1 for r in rows if (r[6] or "").strip())  # last_review 索引=6
        reps = [int(r[8] or 0) for r in rows]  # repetition 索引=8
        rep0 = sum(1 for x in reps if x == 0);
        rep1_2 = sum(1 for x in reps if 1 <= x <= 2);
        rep3p = sum(1 for x in reps if x >= 3)

        # 最近添加/复习
        def _safe_dt(s):
            from datetime import datetime
            try:
                return datetime.fromisoformat(s) if s else None
            except Exception:
                return None

        created = [_safe_dt(r[5]) for r in rows if (r[5] or "").strip()]  # created_at 索引=5
        reviewed = [_safe_dt(r[6]) for r in rows if (r[6] or "").strip()]  # last_review 索引=6
        last_add = max(created).strftime("%Y-%m-%d") if created else "—"
        last_rev = max(reviewed).strftime("%Y-%m-%d") if reviewed else "—"

        return {
            "total": total,
            # 无 fill_rate
            "cnt_reviewed": cnt_reviewed,
            "rep_bins": (rep0, rep1_2, rep3p),
            "last_add": last_add,
            "last_rev": last_rev,
            "cnt_kana": cnt_kana, "cnt_kanji": cnt_kanji, "cnt_mean": cnt_mean
        }

    def _apply_overview(self, stat):
        # 在“总词数”里顺便显示三项计数（不是百分比）
        self.ov_count.setText(
            f"{stat['total']} 词（假名 {stat['cnt_kana']}｜汉字 {stat['cnt_kanji']}｜释义 {stat['cnt_mean']}）")
        r0, r12, r3 = stat['rep_bins']
        self.ov_review.setText(f"已复习 {stat['cnt_reviewed']} 条｜重复 0:{r0} 1-2:{r12} 3+:{r3}")
        self.ov_recent.setText(f"最近 添加/复习：{stat['last_add']} / {stat['last_rev']}")

    def _filtered_rows(self):
        txt = (self.search_box.text() or "").strip().lower()
        rows = self._current_rows_all

        def _hit(r):
            if not txt:
                return True
            term = (r[3] or "").lower()
            mean = (r[4] or "").lower()
            kanji = (r[11] or "").lower() if len(r) > 11 and r[11] else ""
            kana = (r[12] or "").lower() if len(r) > 12 and r[12] else ""
            return (txt in term) or (txt in mean) or (txt in kanji) or (txt in kana)

        rows = [r for r in rows if _hit(r)]

        # 排序
        key = self.sort_combo.currentText()
        from datetime import datetime
        def _ts(s: str) -> float:
            """把各种可能的时间字符串统一成 float 时间戳；无效/空返回 0.0。"""
            if not s:
                return 0.0
            try:
                ss = s.strip().replace("T", " ").replace("Z", "")
                dt = datetime.fromisoformat(ss)  # 兼容 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS'
                return float(dt.timestamp())
            except Exception:
                return 0.0

        if key == "添加时间":
            rows.sort(key=lambda r: _ts(r[5] if len(r) > 5 else ""), reverse=True)  # created_at
        elif key == "上次复习":
            rows.sort(key=lambda r: _ts(r[6] if len(r) > 6 else ""), reverse=True)  # last_review
        elif key == "重复次数":
            rows.sort(key=lambda r: int(r[8] or 0), reverse=True)
        elif key == "易度EF":
            rows.sort(key=lambda r: float(r[9] or 0.0), reverse=True)

        # 打乱
        if getattr(self, "_shuffled", False):
            import random
            random.shuffle(rows)

        return rows

    def _apply_filters_and_refresh(self):
        if not self._current_rows_all:
            return
        self._current_rows_view = self._filtered_rows()
        self.populate_unit_table(self._current_rows_view)

    def _on_shuffle_clicked(self):
        self._shuffled = not self._shuffled
        self._apply_filters_and_refresh()

    # 3) 在 class MainWindow 内新增/替换以下方法
    def _selected_units(self):
        items = self.unit_list.selectedItems()
        seen, units = set(), []
        for it in items:
            t = (it.text() or "").strip()
            if t and t not in seen:
                units.append(t)
                seen.add(t)
        return units

    # 3) 替换 MainWindow._on_add_kana_edited，实现“仅在用户真正改动时关闭自动覆盖”
    def _on_add_kana_edited(self, _text: str):
        # 程序写入期间忽略
        if getattr(self, "_auto_kana_in_progress", False):
            return
        # 只有当与上次自动写入不同，才视为用户自定义，关闭后续自动覆盖
        last = getattr(self, "_last_auto_romaji", "")
        cur = (self.add_kana.text() or "").strip()
        self._kana_autofilled = (cur == "" or cur == last)

    def start_review_selected_units(self):
        # 统一走 open_study_window，从左侧选择读取
        self.open_study_window()

    # 2) 在 class MainWindow 内新增：统一美化下拉框
    def _beautify_combo(self, cb: QtWidgets.QComboBox):
        view = QtWidgets.QListView(cb)
        view.setSpacing(2)
        view.setUniformItemSizes(True)
        cb.setView(view)
        cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        cb.setMinimumContentsLength(6)

    def open_study_window(self, unit_filter=None, include_all=True):
        # 若由按钮 clicked(bool) 触发，Qt 会把一个 bool 作为第一个参数传入，这里忽略之
        if isinstance(unit_filter, bool):
            unit_filter = None

        picked = unit_filter if unit_filter is not None else self._selected_units()
        if not picked:
            QtWidgets.QMessageBox.information(self, "提示", "请在左侧先选择至少一个单元。")
            return

        # 关闭已有复习窗口，避免复用旧过滤条件
        try:
            if getattr(self, "study_win", None) and self.study_win.isVisible():
                self.study_win.close()
        except Exception:
            pass
        self.study_win = None

        # 多选传列表，单选传字符串
        if isinstance(picked, (list, tuple, set)):
            unit_filter_final = list(dict.fromkeys([str(x).strip() for x in picked if str(x).strip()]))
            if len(unit_filter_final) == 1:
                unit_filter_final = unit_filter_final[0]
        else:
            unit_filter_final = picked

        try:
            self.study_win = StudyWindow(self.db, unit_filter=unit_filter_final, include_all=include_all)
            self.study_win.setMinimumSize(960, 640)
            self.study_win.resize(1200, 780)
            screen = QtWidgets.QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                fg = self.study_win.frameGeometry()
                fg.moveCenter(geo.center())
                self.study_win.move(fg.topLeft())
            self.study_win.destroyed.connect(lambda *_: setattr(self, "study_win", None))
            self.study_win.show()
            self.study_win.activateWindow()
            self.study_win.raise_()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "错误", f"启动复习窗口失败：{e}")


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

    # python
    def refresh_study_units(self):
        """
        兼容旧接口：当前学习窗口从左侧列表选择单元，
        无需单独刷新控件，这里留空避免报错。
        """
        pass

    # 2) 替换 MainWindow.refresh_units_to_combo：不再往下拉里添加空项，但保持可编辑、默认文本可为空
    def refresh_units_to_combo(self):
        units = list_units(self.db)
        cur = self.add_unit_combo.currentText() if hasattr(self, 'add_unit_combo') else ""
        self.add_unit_combo.blockSignals(True)
        self.add_unit_combo.clear()
        # 仅添加真实单元，不插入空项
        for u in units:
            self.add_unit_combo.addItem(u)
        # 若之前有输入则恢复；否则保持编辑框为空但不产生空选项
        if cur:
            self.add_unit_combo.setCurrentText(cur)
        else:
            self.add_unit_combo.setEditText("")
        self.add_unit_combo.blockSignals(False)


    def create_unit_dialog(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "新建单元", "单元名：")
        if ok and text.strip():
            self.add_unit_combo.setEditText(text.strip())
            QtWidgets.QMessageBox.information(self, "已设置", "已填入单元名，请继续添加词条。")

    def _restart_mean_timer(self):
        if hasattr(self, "_mean_timer") and self._mean_timer:
            self._mean_timer.stop()
            self._mean_timer.start(300)

    # 替换位置：class MainWindow 方法 _impl_auto_fill_kana_from_term
    def _impl_auto_fill_kana_from_term(self):
        term = (self.add_term.text() or "").strip()
        if not term:
            return

        # 仅对“只有假名、且无汉字”的词条自动转罗马音
        if not (_has_kana(term) and not _has_kanji(term)):
            return

        romaji = kana_to_romaji(term).strip()
        cur = (self.add_kana.text() or "").strip()
        last = getattr(self, "_last_auto_romaji", "")

        # 允许自动覆盖的条件：
        # 1) 未被用户自定义（_kana_autofilled 为 True）
        # 2) 或当前罗马音为空
        # 3) 或当前罗马音仍等于上次自动写入（说明未被用户改动）
        if getattr(self, "_kana_autofilled", True) or cur == "" or cur == last:
            self._auto_kana_in_progress = True
            try:
                self.add_kana.blockSignals(True)
                self.add_kana.setText(romaji)
                self._last_auto_romaji = romaji
                self._kana_autofilled = True
            finally:
                self.add_kana.blockSignals(False)
                self._auto_kana_in_progress = False

    def _auto_fill_kana_from_term(self):
        return self._impl_auto_fill_kana_from_term()

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

    # 替换位置：class MainWindow 方法 _auto_fill_meaning_from_term
    def _auto_fill_meaning_from_term(self, force: bool = False):
        # 用户已手写且非强制，不覆盖
        if not force and (self.add_mean.text() or "").strip():
            return

        term = (self.add_term.text() or "").strip()
        kanji = (self.add_kanji.text() or "").strip()

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
            self._current_unit = None
            self.overview.hide()
        else:
            rows = list_cards_by_unit(self.db, unit)
            self._current_unit = unit
            # 计算并显示总览
            stat = self._calc_overview(rows)
            self._apply_overview(stat)
            self.overview.show()  # 只在单个单元时显示

        # 缓存与刷新
        self._current_rows_all = rows[:]  # 原始
        self._shuffled = False  # 切单元时重置打乱
        self.search_box.clear()  # 清空搜索
        self.sort_combo.setCurrentIndex(0)

        # 过滤并展示
        self._apply_filters_and_refresh()
        self.center_hint.setText(f"当前单元：{unit}（共 {len(rows)} 条）")

    def populate_unit_table(self, rows):
        # 重置并设置行数
        self.unit_table.clearContents()
        self.unit_table.setRowCount(len(rows))

        for row_idx, r in enumerate(rows):
            # 1) 显示用“行号”（从 1 开始），不使用数据库 id
            display_id_item = QtWidgets.QTableWidgetItem(str(row_idx + 1))
            display_id_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.unit_table.setItem(row_idx, 0, display_id_item)

            # 2) 假名（优先显示 term；term 为空则回退 jp_kana）
            kana_src = (r[3] or "").strip()
            if not kana_src:
                # 如 term 为空，回退显示数据库里的 jp_kana（若有）
                kana_src = (r[12] or "").strip() if len(r) > 12 and r[12] else ""
            kana_item = QtWidgets.QTableWidgetItem(kana_src)
            self.unit_table.setItem(row_idx, 1, kana_item)

            # 3) 汉字写法（来自 jp_kanji）
            jp_kanji = (r[11] or "").strip() if len(r) > 11 and r[11] else ""
            kanji_item = QtWidgets.QTableWidgetItem(jp_kanji)
            self.unit_table.setItem(row_idx, 2, kanji_item)

            # 4) 罗马音（仍从 jp_kana 计算）
            jp_kana = (r[12] or "").strip() if len(r) > 12 and r[12] else ""
            romaji = kana_to_romaji(jp_kana) if jp_kana else ""
            romaji_item = QtWidgets.QTableWidgetItem(romaji)
            self.unit_table.setItem(row_idx, 3, romaji_item)

            # 5) 释义
            mean_item = QtWidgets.QTableWidgetItem((r[4] or "").strip())
            self.unit_table.setItem(row_idx, 4, mean_item)

            # 6) 操作（按钮绑定真实数据库 id）
            ops_widget = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(ops_widget)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)

            btn_edit = QtWidgets.QPushButton("编辑")
            btn_edit.setObjectName("mini")
            btn_edit.setProperty("card_id", r[0])  # 真实 DB id
            btn_edit.clicked.connect(self.edit_card_dialog)

            btn_del = QtWidgets.QPushButton("删除")
            btn_del.setObjectName("miniDanger")
            btn_del.setProperty("card_id", r[0])  # 真实 DB id
            btn_del.clicked.connect(self.delete_card_dialog)

            h.addWidget(btn_edit)
            h.addWidget(btn_del)
            h.addStretch()
            self.unit_table.setCellWidget(row_idx, 5, ops_widget)

        # 若需要，显示表格
        if len(rows) > 0:
            self.unit_table.show()

    # 替换位置：class MainWindow 方法 add_card_from_form（去掉语言分支，固定为日语）
    def add_card_from_form(self):
        # 1) 读取表单（固定为“日语”）
        language = "日语"
        unit = (self.add_unit_combo.currentText() or "").strip()
        term = (self.add_term.text() or "").strip()
        meaning = (self.add_mean.text() or "").strip()
        jp_kanji = (self.add_kanji.text() or "").strip() or None
        jp_kana = (self.add_kana.text() or "").strip() or None

        # 2) 兜底：若词条为纯假名且读音未填，则自动带入罗马音
        if (not jp_kana) and _has_kana(term) and not _has_kanji(term):
            try:
                jp_kana = kana_to_romaji(term)
            except Exception:
                pass

        # 3) 校验与补全显示字段
        if not (term or jp_kanji or jp_kana):
            QtWidgets.QMessageBox.warning(self, "缺少内容", "请至少填写 日语词条／汉字写法／假名读音 之一。")
            return
        if not term:
            term = (jp_kanji or jp_kana or "").strip()

        add_card(self.db, language, unit, term, meaning, jp_kanji=jp_kanji, jp_kana=jp_kana)

        # 4) 清空与刷新
        # 4) 清空填写项，但保留单元选择与当前表格视图
        self.add_term.clear()
        self.add_mean.clear()
        self.add_kanji.clear()
        self.add_kana.clear()
        self._kana_autofilled = True

        # 更新“单元”下拉候选，但保留当前编辑文本
        self.refresh_units_to_combo()

        # 如新单元尚未出现在左侧列表，则追加（不改变当前选择）
        if unit and all(self.unit_list.item(i).text() != unit for i in range(self.unit_list.count())):
            self.unit_list.addItem(unit)

        # 刷新当前视图：按左侧当前选中的单元重载数据，但不重置搜索/排序/打乱
        cur_item = self.unit_list.currentItem()
        if cur_item:
            cur = cur_item.text()
            if cur == "所有单元":
                rows = list_cards_by_unit(self.db, None)
                self.overview.hide()
            else:
                rows = list_cards_by_unit(self.db, cur)
                stat = self._calc_overview(rows)
                self._apply_overview(stat)
                self.overview.show()

            self._current_rows_all = rows[:]  # 保持“原始行”
            self._apply_filters_and_refresh()  # 走你已有的搜索/排序/打乱
            self.center_hint.setText(f"当前单元：{cur}（共 {len(rows)} 条）")

        # 让焦点回到“假名”输入，便于继续录入
        self.add_term.setFocus()

        QtWidgets.QMessageBox.information(self, "已添加", "已将该单词加入题库。")

    def add_and_start_unit(self):
        self.add_card_from_form()
        unit = self.add_unit_combo.currentText().strip()
        # 跳转复习窗口（保留原有功能）
        self.open_study_window(unit_filter=(unit if unit else None))


    def delete_card_dialog(self):
        btn = self.sender()
        cid = btn.property("card_id")
        reply = QtWidgets.QMessageBox.question(
            self, "确认删除", "确定删除该词？", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply == QtWidgets.QMessageBox.Yes:
            # 删除真实 DB 记录
            delete_card(self.db, int(cid))
            # 保持当前单元视图并刷新，以“行号”重新编号
            cur_item = self.unit_list.currentItem()
            if cur_item:
                self.on_unit_clicked(cur_item)
            else:
                # 回退：刷新左侧与表格
                self.refresh_units()

    def edit_card_dialog(self):
        btn = self.sender()
        card_id = btn.property("card_id")
        try:
            row = get_card_by_id(self.db, int(card_id))
        except Exception:
            row = None
        if not row:
            QtWidgets.QMessageBox.warning(self, "未找到", "未能读取该词条，可能已被删除。")
            return

        dlg = EditDialog(self, self.db, row)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            try:
                language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby = dlg.get_result()
                update_card_fields_full(
                    self.db, row[0], language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby
                )
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "保存失败", f"写入数据库失败：{e}")
                return

            # 刷新当前视图
            cur_item = self.unit_list.currentItem()
            cur_unit = cur_item.text() if cur_item else "所有单元"
            try:
                if cur_unit == "所有单元":
                    rows = list_cards_by_unit(self.db, None)
                else:
                    rows = list_cards_by_unit(self.db, cur_unit)
                self.populate_unit_table(rows)
                self.center_hint.setText(f"当前单元：{cur_unit}（共 {len(rows)} 条）")
            except Exception:
                # 保底刷新
                self.refresh_units()

            QtWidgets.QMessageBox.information(self, "已保存", "词条已更新。")

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

    def _open_unit_overview(self):
        if not getattr(self, "_current_unit", None):
            QtWidgets.QMessageBox.information(self, "提示", "请先在左侧选中一个【单个】单元。")
            return
        rows = getattr(self, "_current_rows_all", None)
        if not rows:
            QtWidgets.QMessageBox.information(self, "提示", "该单元暂无单词。")
            return
        dlg = UnitOverviewDialog(self, self._current_unit, rows)
        dlg.exec_()

class UnitOverviewDialog(QtWidgets.QDialog):
    def __init__(self, parent, unit_name: str, rows: list):
        super().__init__(parent)
        self.setWindowTitle(f"总览 - {unit_name}（{len(rows)} 条）")
        self.resize(900, 560)

        layout = QtWidgets.QVBoxLayout(self)

        tip = QtWidgets.QLabel("双列展示：假名 / 汉字写法；并列出罗马音与释义。可直接滚动背诵。")
        tip.setObjectName("muted")
        layout.addWidget(tip)

        table = QtWidgets.QTableWidget(self)
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["ID", "假名", "汉字写法", "罗马音", "释义", "重复次数"])
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)

        table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        table.setAlternatingRowColors(True)

        table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            # 约定索引：0:id, 3:term(假名), 4:释义, 11:jp_kanji, 12:jp_kana, 8:repetition
            jid = QtWidgets.QTableWidgetItem(str(r[0] if r[0] is not None else ""))
            term_kana = (r[3] or "").strip()
            jp_kanji  = (r[11] or "").strip() if len(r)>11 and r[11] else ""
            jp_kana   = (r[12] or "").strip() if len(r)>12 and r[12] else ""
            romaji    = kana_to_romaji(jp_kana) if jp_kana else ""
            mean      = (r[4] or "").strip()
            rep       = str(int(r[8] or 0)) if len(r)>8 else "0"

            table.setItem(i, 0, jid)
            table.setItem(i, 1, QtWidgets.QTableWidgetItem(term_kana))
            table.setItem(i, 2, QtWidgets.QTableWidgetItem(jp_kanji))
            table.setItem(i, 3, QtWidgets.QTableWidgetItem(romaji))
            table.setItem(i, 4, QtWidgets.QTableWidgetItem(mean))
            table.setItem(i, 5, QtWidgets.QTableWidgetItem(rep))

        layout.addWidget(table)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch()
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

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

        # 3) EditDialog：取消语言下拉，固定为“日语”
        # 在 EditDialog.__init__ 里，删除原有 self.cb_lang 相关代码，替换为只展示标签
        lbl_lang = QtWidgets.QLabel("日语")
        form.addRow("语言", lbl_lang)

        # 单元（下拉选择，不手动输入）
        self.cb_unit = QtWidgets.QComboBox()
        self.cb_unit.setEditable(False)
        units = list_units(conn)
        self.cb_unit.addItem("")  # 允许空单元
        for u in units:
            self.cb_unit.addItem(u)
        cur_unit = (row[2] or "").strip()
        if self.cb_unit.findText(cur_unit) < 0 and cur_unit:
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
        form.addRow("罗马音", self.ed_kana)

        # 中文释义
        self.ed_mean = QtWidgets.QLineEdit(row[4] or "")
        form.addRow("中文释义", self.ed_mean)

        layout.addLayout(form)

        # 按钮区
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        for cb in (getattr(self, "cb_unit", None),):
            if cb:
                cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)

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
        kana = (self.ed_kana.text() or "").strip()
        if not term and not kanji and not kana:
            QtWidgets.QMessageBox.warning(self, "校验失败", "请至少填写“日语词条”或“汉字写法/假名读音”中的一项。")
            return
        self.accept()

    # 4) EditDialog：统一返回固定语言（确保 MainWindow.edit_card_dialog 中 get_result 正常）
    # 若类中尚无 get_result，则新增；若已存在，则替换为以下实现
    def get_result(self):
        language = "日语"
        unit = (self.cb_unit.currentText() or "").strip()
        term = (self.ed_term.text() or "").strip()
        meaning = (self.ed_mean.text() or "").strip()
        jp_kanji = (self.ed_kanji.text() or "").strip() or None
        jp_kana = (self.ed_kana.text() or "").strip() or None
        # 兼容可选的 jp_ruby
        jp_ruby = None
        return language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby


class StudyWindow(QtWidgets.QWidget):
    def __init__(self, db, unit_filter=None, include_all=False):
        super().__init__()
        self.db = db
        self.unit_filter = unit_filter
        self.include_all = bool(include_all)

        if unit_filter is None or unit_filter == "" or unit_filter == "所有单元":
            title_units = "全部单元"
        elif isinstance(unit_filter, (list, tuple, set)):
            title_units = f"{len(unit_filter)} 个单元"
        else:
            title_units = str(unit_filter)
        self.setWindowTitle(f"复习 - {title_units}")

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
        # 单元归一：None 表示所有单元
        unit = getattr(self, 'unit_filter', None)
        if unit in ("所有单元", "", None):
            unit = None

        # 调试打印，便于核对传入条件和数据量
        try:
            cur = self.db.cursor()
            total = cur.execute('SELECT COUNT(*) FROM cards').fetchone()[0]
            print(f"[Study] unit={unit!r}, include_all={getattr(self, 'include_all', False)}, total_cards={total}")
        except Exception:
            pass

        # 1) 优先按“不过滤到期”或“无到期时回退到全部”
        if getattr(self, 'include_all', False):
            rows = list_cards_by_unit(self.db, unit=unit)
        else:
            rows = list_due_cards(self.db, unit=unit)
            if not rows:  # 无到期则回退到“全部”
                rows = list_cards_by_unit(self.db, unit=unit)

        # 额外兜底：若是按某单元取不到，退回所有单元
        if not rows and unit is not None:
            rows = list_cards_by_unit(self.db, unit=None)

        # 2) 题库真的为空才提示
        self.queue = []
        n = len(rows)
        if n == 0:
            QtWidgets.QMessageBox.information(self, "提示", "题库为空，请先添加单词。")
            self.idx = -1
            return

        # 3) 组卷：日→中 70% / 中→日 30%
        num_mode0 = int(n * 0.7)  # 模式0：日→中
        num_mode1 = n - num_mode0  # 模式1：中→日
        modes = [0] * num_mode0 + [1] * num_mode1
        random.shuffle(modes)

        for r, m in zip(rows, modes):
            self.queue.append((r, m))

        # 4) 开始第一题
        self.idx = 0
        self.show_card(self.queue[self.idx])

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