# vocab_trainer_units_v2_modern_ui.py
# 改进版 UI（功能不变）：更现代的布局与样式
# 运行: python vocab_trainer_units_v2_modern_ui.py
# 依赖: pip install PyQt5

import sys
import os
import sqlite3
import csv
import random
from datetime import datetime, timedelta
from PyQt5 import QtWidgets, QtCore, QtGui

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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    # ensure 'unit' exists (back-compat)
    cols = [c[1] for c in cur.execute('PRAGMA table_info(cards)').fetchall()]
    if 'unit' not in cols:
        try:
            cur.execute('ALTER TABLE cards ADD COLUMN unit TEXT DEFAULT ""')
            conn.commit()
        except Exception:
            pass
    return conn

# --------------------------
# DB 操作
# --------------------------
def add_card(conn, language, unit, term, meaning):
    now = datetime.utcnow().isoformat()
    # 不在录入时设置人为到期：仍写入 due_date = created_at，内部用时间判定（本版本保持与原功能一致）
    due = None
    cur = conn.cursor()
    cur.execute('''
    INSERT INTO cards (language, unit, term, meaning, created_at, last_review, interval, repetition, ef, due_date)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (language, unit, term, meaning, now, None, 0, 0, 2.5, due))
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

def import_from_csv(conn, csv_path, default_unit=""):
    added = 0
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            # 支持 language,unit,term,meaning 或 language,term,meaning
            if len(row) >= 4:
                language, unit, term, meaning = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
            else:
                language, term, meaning = row[0].strip(), row[1].strip(), (row[2].strip() if len(row) > 2 else "")
                unit = default_unit
            if term:
                add_card(conn, language or "日语", unit or "", term, meaning)
                added += 1
    return added

def export_to_csv(conn, csv_path, unit=None):
    rows = list_cards_by_unit(conn, unit)
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'language', 'unit', 'term', 'meaning', 'created_at', 'last_review', 'interval', 'repetition', 'ef', 'due_date'])
        for r in rows:
            writer.writerow(r)
    return len(rows)

# --------------------------
# SM-2 算法（保持不变）
# --------------------------
def sm2_update(card_row, quality):
    _id, language, unit, term, meaning, created_at, last_review, interval, repetition, ef, due_date = card_row
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

# --------------------------
# 工具：解析 ISO 时间
# --------------------------
def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
        except:
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

DARK_STYLE = APP_STYLE + """
/* 深色覆盖 */
QMainWindow { background: #0f172a; }
QWidget { color: #e5e7eb; }
QFrame#panel { background: #111827; border: 1px solid #1f2937; }
QLineEdit, QComboBox, QTextEdit {
    background: #0b1220; border: 1px solid #334155; color: #e5e7eb;
}
QListWidget#unitList { background:#0b1220; border:1px solid #334155; }
QListWidget#unitList::item { border-bottom: 1px solid #1f2937; }
QListWidget#unitList::item:selected { background: #1f2a44; color: #e5e7eb; }

QTableWidget { background:#0b1220; border:1px solid #334155; gridline-color:#1f2937; }
QHeaderView::section { background:#0b1220; color:#e5e7eb; border-bottom:1px solid #334155; }
QTableCornerButton::section { background:#0b1220; border-bottom:1px solid #334155; }

QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2563eb, stop:1 #3b82f6); }
QPushButton#secondary { background:#0b1220; color:#e5e7eb; border:1px solid #334155; }
QPushButton#mini { background:#1f2937; color:#e5e7eb; }
QPushButton#miniDanger { background:#7f1d1d; color:#fecaca; }
QLabel#bigterm { color:#e5e7eb; }
QLabel#muted { color:#9ca3af; }
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
        self.setWindowTitle("词汇训练器 v2（单元优先）")
        self.resize(1180, 760)
        self.setMinimumSize(980, 620)
        QtWidgets.QApplication.instance().setStyleSheet(APP_STYLE)
        self.dark_mode = False  # 记录当前主题

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 顶部标题栏
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("词汇训练器")
        title.setObjectName("appTitle")
        header.addWidget(title)
        header.addStretch()
        tools = QtWidgets.QHBoxLayout()
        tools.setSpacing(6)
        root.addLayout(header)

        # 导入 CSV（图标按钮）
        btn_import = QtWidgets.QToolButton()
        btn_import.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogOpenButton))
        btn_import.setToolTip("导入 CSV")
        btn_import.clicked.connect(self.import_csv_dialog)
        tools.addWidget(btn_import)

        # 导出 CSV（图标按钮）
        btn_export = QtWidgets.QToolButton()
        btn_export.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogSaveButton))
        btn_export.setToolTip("导出 CSV")
        btn_export.clicked.connect(self.export_csv_dialog)
        tools.addWidget(btn_export)

        # 主题切换（图标按钮）
        self.btn_theme = QtWidgets.QToolButton()
        self.btn_theme.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TitleBarShadeButton))
        self.btn_theme.setToolTip("切换深色主题")
        self.btn_theme.clicked.connect(self.toggle_theme)
        tools.addWidget(self.btn_theme)

        header.addLayout(tools)

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

        # 添加区域
        add_row = QtWidgets.QHBoxLayout()
        add_row.setSpacing(12)

        form_col = QtWidgets.QFormLayout()
        form_col.setLabelAlignment(QtCore.Qt.AlignRight)

        self.add_language = QtWidgets.QComboBox()
        self.add_language.setEditable(True)
        self.add_language.addItems(["日语", "韩语", "其他"])
        form_col.addRow("语言", self.add_language)

        self.add_unit_combo = QtWidgets.QComboBox()
        self.add_unit_combo.setEditable(True)
        self.refresh_units_to_combo()
        form_col.addRow("单元", self.add_unit_combo)

        self.add_term = QtWidgets.QLineEdit()
        form_col.addRow("日语词条", self.add_term)

        self.add_mean = QtWidgets.QLineEdit()
        form_col.addRow("中文释义", self.add_mean)

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
        self.unit_table.setHorizontalHeaderLabels(["ID", "语言", "词条", "释义", "操作"])
        header = self.unit_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
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
        # restore
        idx = self.add_unit_combo.findText(cur)
        if idx >= 0:
            self.add_unit_combo.setCurrentIndex(idx)
        else:
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
            # 不创建空卡，仅更新下拉框方便直接录入
            self.add_unit_combo.setEditText(text.strip())
            self.refresh_units_to_combo()

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
        self.unit_table.setRowCount(0)
        for r in rows:
            row_idx = self.unit_table.rowCount()
            self.unit_table.insertRow(row_idx)
            id_item = QtWidgets.QTableWidgetItem(str(r[0])); id_item.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
            lang_item = QtWidgets.QTableWidgetItem(r[1] or "")
            term_item = QtWidgets.QTableWidgetItem(r[3] or "")
            mean_item = QtWidgets.QTableWidgetItem(r[4] or "")

            for col, it in enumerate([id_item, lang_item, term_item, mean_item]):
                self.unit_table.setItem(row_idx, col, it)

            # 操作列：编辑 / 删除（精简小按钮）
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

    # ---------- 添加卡片 ----------
    def add_card_from_form(self):
        language = self.add_language.currentText().strip() or "日语"
        unit = self.add_unit_combo.currentText().strip()
        term = self.add_term.text().strip()
        meaning = self.add_mean.text().strip()
        if not term:
            QtWidgets.QMessageBox.warning(self, "输入错误", "请填写日语词条。")
            return
        add_card(self.db, language, unit, term, meaning)
        self.add_term.clear(); self.add_mean.clear()
        self.refresh_units()
        QtWidgets.QMessageBox.information(self, "已添加", "已将该单词加入题库。")

    def add_and_start_unit(self):
        self.add_card_from_form()
        unit = self.add_unit_combo.currentText().strip()
        # 跳转复习窗口（保留原有功能）
        self.open_study_window(unit_filter=(unit if unit else None))

    # ---------- 编辑 / 删除 ----------
    def edit_card_dialog(self):
        btn = self.sender()
        cid = btn.property("card_id")
        cur = self.db.cursor()
        cur.execute('SELECT * FROM cards WHERE id = ?', (cid,))
        row = cur.fetchone()
        if not row:
            return
        dlg = EditDialog(self, row)
        if dlg.exec_():
            lang, unit, term, mean = dlg.get_values()
            update_card_fields(self.db, cid, lang, unit, term, mean)
            self.refresh_units()

    def delete_card_dialog(self):
        btn = self.sender()
        cid = btn.property("card_id")
        reply = QtWidgets.QMessageBox.question(self, "确认删除", "确定删除该词？", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if reply == QtWidgets.QMessageBox.Yes:
            delete_card(self.db, cid)
            self.refresh_units()

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

    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        app = QtWidgets.QApplication.instance()
        if self.dark_mode:
            app.setStyleSheet(DARK_STYLE)
            self.btn_theme.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TitleBarUnshadeButton))
            self.btn_theme.setToolTip("切换浅色主题")
        else:
            app.setStyleSheet(APP_STYLE)
            self.btn_theme.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TitleBarShadeButton))
            self.btn_theme.setToolTip("切换深色主题")

    def import_csv_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择要导入的 CSV", os.path.expanduser("~"),
                                                        "CSV (*.csv)")
        if not path:
            return
        try:
            n = import_from_csv(self.db, path)
            self.refresh_units()
            QtWidgets.QMessageBox.information(self, "导入完成", f"已导入 {n} 条。")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导入失败", f"错误：{e}")

    def export_csv_dialog(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出到 CSV",
            os.path.join(os.path.expanduser("~"), "export.csv"),
            "CSV (*.csv)"
        )
        if not path:
            return
        try:
            current_item = self.unit_list.currentItem()
            unit = None
            if current_item and current_item.text() != "所有单元":
                unit = current_item.text()
            n = export_to_csv(self.db, path, unit=unit)
            QtWidgets.QMessageBox.information(self, "导出完成", f"已导出 {n} 条到：\n{path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导出失败", f"错误：{e}")


# ---------- 编辑对话框 ----------
class EditDialog(QtWidgets.QDialog):
    def __init__(self, parent, row):
        super().__init__(parent)
        self.setWindowTitle("编辑词条")
        self.setMinimumWidth(420)
        self.dark_mode = False  # 记录当前主题
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        form_frame = QtWidgets.QFrame()
        form_frame.setObjectName("panel")
        apply_shadow(form_frame, radius=12, offset=(0,3))
        form_l = QtWidgets.QFormLayout(form_frame)
        form_l.setContentsMargins(12, 12, 12, 12)
        form_l.setLabelAlignment(QtCore.Qt.AlignRight)

        self.lang = QtWidgets.QLineEdit(row[1] or "")
        self.unit = QtWidgets.QLineEdit(row[2] or "")
        self.term = QtWidgets.QLineEdit(row[3] or "")
        self.mean = QtWidgets.QLineEdit(row[4] or "")

        form_l.addRow("语言：", self.lang)
        form_l.addRow("单元：", self.unit)
        form_l.addRow("日语词条：", self.term)
        form_l.addRow("中文释义：", self.mean)

        layout.addWidget(form_frame)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        layout.addWidget(btns)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
    def get_values(self):
        return self.lang.text().strip(), self.unit.text().strip(), self.term.text().strip(), self.mean.text().strip()

class StudyWindow(QtWidgets.QWidget):
    def __init__(self, db, unit_filter=None):
        super().__init__()
        self.db = db
        self.unit_filter = unit_filter
        self.setWindowTitle(f"复习 - {'全部单元' if not unit_filter else unit_filter}")
        self.resize(820, 560)
        QtWidgets.QApplication.instance().setStyleSheet(APP_STYLE)
        self.dark_mode = False  # 记录当前主题

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

        self.term_label = QtWidgets.QLabel("——")
        self.term_label.setObjectName("bigterm")
        self.term_label.setAlignment(QtCore.Qt.AlignCenter)
        card_layout.addWidget(self.term_label)

        self.mean_label = QtWidgets.QLabel("")
        self.mean_label.setAlignment(QtCore.Qt.AlignCenter)
        self.mean_label.setWordWrap(True)
        self.mean_label.setStyleSheet("color:#4b5563;")  # muted
        card_layout.addWidget(self.mean_label)

        self.btn_toggle = QtWidgets.QPushButton("显示 / 隐藏 释义")
        self.btn_toggle.setObjectName("secondary")
        self.btn_toggle.clicked.connect(self.toggle_meaning)
        self.btn_toggle.setFixedWidth(160)
        card_layout.addWidget(self.btn_toggle, 0, QtCore.Qt.AlignHCenter)

        # 评分按钮
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
        self.btn_again.clicked.connect(lambda: self.submit_rating(1))
        self.btn_hard.clicked.connect(lambda: self.submit_rating(3))
        self.btn_good.clicked.connect(lambda: self.submit_rating(4))
        self.btn_easy.clicked.connect(lambda: self.submit_rating(5))
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

    def prepare_queue(self):
        # 取出所有到期卡片（保持与原功能一致）
        rows = list_due_cards(self.db, unit=self.unit_filter)
        self.queue = []

        n = len(rows)
        if n == 0:
            return

        # 7:3比例
        num_mode0 = int(n * 0.7)  # 日语->中文
        num_mode1 = n - num_mode0  # 中文->日语

        # 生成模式列表
        modes = [0] * num_mode0 + [1] * num_mode1
        random.shuffle(modes)

        # 生成 queue
        for r, m in zip(rows, modes):
            self.queue.append((r, m))

    def show_card(self, card):
        row, mode = card
        self.current = (row, mode)
        self.showing_mean = False
        self.mean_label.setText("")

        # 清理旧控件
        if hasattr(self, 'input_answer') and self.input_answer:
            self.input_answer.hide()
        if hasattr(self, 'btn_confirm') and self.btn_confirm:
            self.btn_confirm.hide()

        if mode == 0:  # 日语→中文
            self.term_label.setText(row[3])
            for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                b.setEnabled(True)
            self.continue_btn.setEnabled(False)
        else:  # 中文→日语
            self.term_label.setText(row[4])
            # 创建输入框和确认按钮（放入卡片内）
            self.input_answer = QtWidgets.QLineEdit()
            self.input_answer.setPlaceholderText("请输入日语答案")
            self.btn_confirm = QtWidgets.QPushButton("确认答案")
            self.btn_confirm.setObjectName("secondary")
            self.btn_confirm.clicked.connect(self.check_answer)
            parent_card_layout = self.layout().itemAt(1).widget().layout()  # card_layout
            parent_card_layout.addWidget(self.input_answer)
            parent_card_layout.addWidget(self.btn_confirm)
            self.input_answer.show()
            self.btn_confirm.show()
            for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                b.setEnabled(False)
            self.continue_btn.setEnabled(False)

    def check_answer(self):
        user_input = self.input_answer.text().strip()
        correct = self.current[0][3]  # 日语
        if user_input == correct:
            self.mean_label.setText("回答正确！")
            quality = 5
        else:
            self.mean_label.setText(f"你的答案: {user_input}\n正确答案: {correct}")
            quality = 3

        # 更新 SM-2 数据
        interval, repetition, ef = sm2_update(self.current[0], quality)
        last_review = datetime.utcnow().isoformat()
        due_date = None
        update_card_review(self.db, self.current[0][0], interval, repetition, ef, last_review, due_date)

        # 启用继续按钮
        self.continue_btn.setEnabled(True)
        self.input_answer.hide()
        self.btn_confirm.hide()

    def toggle_meaning(self):
        if not self.current:
            return
        row, mode = self.current
        if self.showing_mean:
            self.mean_label.setText("")
            self.showing_mean = False
        else:
            self.mean_label.setText(row[4] if mode == 0 else row[3])
            self.showing_mean = True

    def submit_rating(self, quality):
        if not self.current:
            return
        row, mode = self.current
        interval, repetition, ef = sm2_update(row, quality)
        last_review = datetime.utcnow().isoformat()
        due_date = None
        update_card_review(self.db, row[0], interval, repetition, ef, last_review, due_date)

        for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
            b.setEnabled(False)

        self.session_total += 1
        if quality >= 4:
            self.session_correct += 1
        self.continue_btn.setEnabled(True)
        self.mean_label.setText(row[4] if mode == 0 else row[3])
        self.showing_mean = True
        self.info_label.setText(f"已做 {self.session_total}，正确 {self.session_correct}")

    def next_card(self):
        self.idx += 1
        if self.idx >= len(self.queue):
            QtWidgets.QMessageBox.information(
                self, "本轮结束",
                f"本轮复习结束\n共 {self.session_total} 项，正确 {self.session_correct}"
            )
            self.close()
            return
        self.show_card(self.queue[self.idx])

    def try_space_continue(self):
        if self.continue_btn.isEnabled():
            self.next_card()

def main():
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)  # 可选：全局样式初始化
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()