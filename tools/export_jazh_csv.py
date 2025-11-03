# python
# 导出 my_ja_zh.json 为本地 JA-ZH CSV（term,kana,meaning）
# 解析要点：
# 1) 开头的（假名）或(假名) -> kana
# 2) 紧随其后的圈号⓪/①… -> pitch（丢弃）
# 3) 紧随其后的【…】 -> 词性（丢弃）
# 4) 余下为中文释义
from __future__ import annotations

import json
import csv
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
IN_JSON = HERE / 'my_ja_zh.json'
OUT_CSV = HERE / 'vocab_local_jazh.csv'

RE_LEADING_PARENS = re.compile(r'^\s*[（(]\s*([^（）\(\)]+?)\s*[)）]\s*')
RE_LEADING_CIRCLED = re.compile(r'^\s*[⓪①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]+\s*')
RE_LEADING_POS = re.compile(r'^\s*【([^】]+)】\s*')
# 新增：剥离括号内读音末尾的音高标注（半角/全角数字，或圈号数字）
RE_TRAILING_PITCH = re.compile(r'(?:[0-9\uFF10-\uFF19]+|[⓪①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])+$')

# 允许的假名字符（平/片/长音/中点/半角片假名/小假名）
def is_kana(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if ch.isspace():
            continue
        if (
            0x3040 <= code <= 0x309F or  # ひらがな
            0x30A0 <= code <= 0x30FF or  # カタカナ
            0x31F0 <= code <= 0x31FF or  # 小假名
            0xFF66 <= code <= 0xFF9D or  # 半角片假名
            ch in ('ー', '・')
        ):
            continue
        return False
    return True

def parse_value(val: str) -> tuple[str, str, str]:
    """
    返回: (kana, pos, meaning_zh)
    """
    text = (val or '').strip()

    kana = ''
    pos = ''

    # 1) 起始括号读假名（全角/半角）
    m = RE_LEADING_PARENS.match(text)
    if m:
        kana_raw = m.group(1).strip()
        # 剥离末尾音高：如 ひとつ2／ひとりひとり0／トオ1 等末尾数字或圈号
        kana = RE_TRAILING_PITCH.sub('', kana_raw).strip()
        text = text[m.end():].lstrip()

    # 2) 去掉圈号数字（若有）
    m = RE_LEADING_CIRCLED.match(text)
    if m:
        text = text[m.end():].lstrip()

    # 3) 去掉词性【…】（若有）
    m = RE_LEADING_POS.match(text)
    if m:
        pos = m.group(1).strip()
        text = text[m.end():].lstrip()

    meaning = text.strip()
    return kana, pos, meaning

def main():
    if not IN_JSON.exists():
        raise SystemExit(f'未找到输入文件: {IN_JSON}')

    data = json.loads(IN_JSON.read_text(encoding='utf-8'))
    rows = []
    skipped = 0

    for term, val in data.items():
        term_str = str(term).strip()
        if not term_str:
            skipped += 1
            continue

        kana, pos, meaning = parse_value(str(val))

        # 若没解析到假名且“键”为纯假名，用键填充假名
        if not kana and is_kana(term_str):
            kana = term_str

        # 清洗释义首尾的无关冒号/空格
        meaning = meaning.strip('：: \t\r\n')

        # 最少要有中文释义，若没有则跳过
        if not meaning:
            skipped += 1
            continue

        # 导出最通用的三列：term,kana,meaning（应用仅需前两列或前两列+meaning）
        rows.append((term_str, kana, meaning))

    # Windows/Excel 友好：UTF-8 BOM
    with OUT_CSV.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['term', 'kana', 'meaning'])
        writer.writerows(rows)

    print(f'已写出 {len(rows)} 条到 {OUT_CSV}（跳过 {skipped} 条）')

if __name__ == '__main__':
    main()