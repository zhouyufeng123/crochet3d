"""钩织图解 docx 解析器。

从 Word 图解文档提取：材料（用线/钩针）、针法、部件、每圈表达式与针数。
针数计算规则（中文图解惯例）：
    x/X 短针=1   v/V 加针=2   A 减针=1(两针并一针)   w=3
    T 中长针=1   F 长针=1     E 长长针=1             B 爆米花针=1
    FV 长针加针=2  FA 长针减针=1
    ch 锁针/SL 引拔=0（不计入针数）
    前置重复: 4X=4个短针, 3(x,v)=9; 后置重复: (3f,0)*5=整组×5
"""

import re
import zipfile
from pathlib import Path

STITCH_VALUES = {
    "x": 1, "v": 2, "a": 1, "w": 3,
    "t": 1, "f": 1, "e": 1, "b": 1,
    "fv": 2, "fa": 1, "tv": 2,
}

ROUND_RE = re.compile(r"^R\s*(\d+)\s*(?:[-–~]\s*R?(\d+))?\s*[：:]\s*(.+)$")
ROW_RE = re.compile(r"^第\s*([0-9一二三四五六七八九十百]+)\s*[圈行]\s*[:：]\s*(.+)$")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    # 简单中文数字（十几、二十几）
    if s == "十":
        return 10
    m = re.fullmatch(r"(十|[二三四五六七八九]?十)([一二三四五六七八九])?", s)
    if m:
        tens = 1 if m.group(1) == "十" else CN_NUM[m.group(1)[0]]
        ones = CN_NUM[m.group(2)] if m.group(2) else 0
        return tens * 10 + ones
    return CN_NUM.get(s, 0)


def extract_text(docx_path) -> list[str]:
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    lines = []
    for para in paras:
        texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para)
        line = "".join(texts).strip()
        if line:
            lines.append(line)
    return lines


PROSE_MARKERS = re.compile(r"个短针|个加针|个减针|一组花样|花样|与本圈|断线|备用|塞棉|辫子|引拔")


def _clean_expr(raw: str) -> str:
    """表达式清洗：去说明、替换不计针数项、统一分隔符。"""
    s = raw
    # 括号归一
    s = s.replace("[", "(").replace("]", ")").replace("【", "(").replace("】", ")")
    s = s.replace("〔", "(").replace("〕", ")")
    # 部件连接说明（链接耳朵1 等，含名字里的数字）
    s = re.sub(r"链接?[^,，:：()]*", "", s)
    # 不计针数项 → 0
    s = re.sub(r"\d*\s*个?\s*(锁针|辫子)", "0", s)
    s = re.sub(r"\d*\s*ch", "0", s, flags=re.I)
    s = re.sub(r"(引拔|\bsl\b|sI|s1)", "0", s, flags=re.I)
    s = re.sub(r"起立", "0", s)
    # 中文括号说明（换色 等）→ 分隔
    s = re.sub(r"\([^)]*[\u4e00-\u9fff][^)]*\)", ",", s)
    s = re.sub(r"（[^）]*[\u4e00-\u9fff][^）]*）", ",", s)
    # 表达式在第一个中文字符处结束（后面是钩法说明散文）
    m = re.search(r"[\u4e00-\u9fff]", s)
    if m:
        head, tail = s[: m.start()], s[m.start():]
        if head.strip(", ") and head.count("(") == head.count(")"):
            s = head
        else:
            # 表达式以中文开头或括号不平衡：全部中文换成分隔符
            s = re.sub(r"[\u4e00-\u9fff]", ",", s)
    # 全角→半角、分隔符统一
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("，", ",").replace("、", "+").replace("×", "*").replace("＊", "*")
    s = s.replace("＋", "+").replace(".", ",").replace("。", ",")
    s = re.sub(r"[^\dA-Za-z()+*,*]", "", s)
    s = re.sub(r",+", ",", s).strip(",+ ")
    return s


def _eval_expr(s: str):
    if not s:
        return None

    pos = 0

    def peek():
        return s[pos] if pos < len(s) else ""

    def parse_term():
        nonlocal pos
        c = peek()
        if c == "":
            return None
        if c == "(":
            pos += 1
            val = parse_seq()
            if val is None:
                return None
            if peek() != ")":
                return None
            pos += 1
            if peek().isdigit():
                m2 = re.match(r"\d+", s[pos:])
                pos += len(m2.group(0))
                val = val * int(m2.group(0))
            return val
        if c.isdigit():
            m = re.match(r"\d+", s[pos:])
            num = int(m.group(0))
            pos += len(m.group(0))
            nxt = peek()
            if nxt == "*":
                pos += 1
                inner = parse_term()
                return None if inner is None else num * inner
            if nxt == "(":
                # 不消费括号，交给括号分支求整组值
                inner = parse_term()
                return None if inner is None else num * inner
            if nxt.isalpha():
                st = _match_stitch()
                return None if st is None else num * st
            return num if num == 0 else None
        if c.isalpha():
            st = _match_stitch()
            return st
        return None

    def _match_stitch():
        nonlocal pos
        two = s[pos : pos + 2].lower()
        if two in STITCH_VALUES:
            pos += 2
            return STITCH_VALUES[two]
        one = s[pos].lower()
        if one in STITCH_VALUES:
            pos += 1
            return STITCH_VALUES[one]
        return None

    def parse_seq():
        nonlocal pos
        total = 0
        while True:
            if peek() == "," and pos + 1 < len(s) and s[pos + 1] == ")":
                pos += 1  # 容忍尾逗号 (2x,v,)
                continue
            val = parse_term()
            if val is None:
                return None
            total += val
            if peek() in (",", "+"):
                pos += 1
                continue
            if peek() in ("", ")"):
                return total
            return None

    # 后置整组重复: (3f,0)*5 或 ...*5
    m = re.search(r"\*(\d+)$", s)
    mult = 1
    if m:
        s = s[: m.start()]
        mult = int(m.group(1))
    val = parse_seq()
    if val is None:
        return None
    return val * mult


def parse_round_expr(expr: str):
    s = _clean_expr(expr)
    if not s:
        return None
    return _eval_expr(s)


def parse_pattern(docx_path) -> dict:
    path = Path(docx_path)
    lines = extract_text(path)

    name = re.sub(r"图解\d*（?详细版）?", "", path.stem).strip("()（） 1234567890") or path.stem
    yarn = hook = ""
    stitches_intro = ""
    parts: list[dict] = []
    current = {"name": "主体", "rounds": []}

    for line in lines:
        if not yarn:
            m = re.search(r"用线\s*[:：]\s*(\S+)", line)
            if m:
                yarn = m.group(1)
        if not hook:
            m = re.search(r"钩针\s*[:：]\s*(\S+)", line)
            if m:
                hook = m.group(1)

        m = ROUND_RE.match(line) or ROW_RE.match(line)
        if m:
            if m.group(0).startswith("第"):
                r1 = _cn_to_int(m.group(1))
                r2 = r1
                expr = m.group(2).strip()
            else:
                r1 = int(m.group(1))
                r2 = int(m.group(2)) if m.group(2) else r1
                expr = m.group(3).strip()
            # 表达式在第二个冒号处截断（第N圈: 表达式：中文说明）
            m2 = re.match(r"([^:：]+)[：:]", expr)
            if m2 and m2.group(1).strip():
                expr = m2.group(1)
            # 中文说明行（与 R 行成对出现）跳过
            if PROSE_MARKERS.search(expr):
                continue
            count = parse_round_expr(expr)
            current["rounds"].append({"r": r1, "rEnd": r2, "expr": expr, "count": count})
            continue

        if (
            not line.startswith(("一", "二", "三", "四", "第", "（", "("))
            and len(line) <= 24
            and not line.startswith("R")
            and re.search(r"[A-Za-z0-9\u4e00-\u9fff]", line)
            and not re.search(r"[。；;,，]", line)
        ):
            if current["rounds"]:
                parts.append(current)
            current = {"name": line.strip(), "rounds": []}

    if current["rounds"]:
        parts.append(current)

    # 同一圈号去重：优先保留可解析且更短的写法
    for part in parts:
        seen = {}
        dedup = []
        for rd in part["rounds"]:
            key = rd["r"]
            prev = seen.get(key)
            if prev is None:
                seen[key] = rd
                dedup.append(rd)
                continue
            if rd["count"] is not None and (prev["count"] is None or len(rd["expr"]) < len(prev["expr"])):
                dedup[dedup.index(prev)] = rd
                seen[key] = rd
        part["rounds"] = dedup

    total = 0
    unparsed = 0
    for part in parts:
        ptotal = 0
        for rd in part["rounds"]:
            span = rd["rEnd"] - rd["r"] + 1
            if rd["count"] is None:
                unparsed += 1
                continue
            ptotal += rd["count"] * span
        # 部件名里的 *2 / ×2 表示要钩 N 个
        mp = re.search(r"[*×]\s*(\d+)", part["name"])
        part["pieces"] = int(mp.group(1)) if mp else 1
        part["total"] = ptotal * part["pieces"]
        total += part["total"]

    return {
        "id": path.stem,
        "name": name,
        "file": path.name,
        "yarn": yarn,
        "hook": hook,
        "stitchTypes": stitches_intro,
        "parts": parts,
        "total": total,
        "unparsed": unparsed,
        "roundCount": sum(rd["rEnd"] - rd["r"] + 1 for p in parts for rd in p["rounds"]),
    }


def parse_folder(folder) -> list[dict]:
    folder = Path(folder)
    results = []
    for p in sorted(folder.glob("*.docx")):
        if p.name.startswith("~$"):
            continue
        try:
            results.append(parse_pattern(p))
        except Exception as exc:
            results.append(
                {"id": p.stem, "name": p.stem, "file": p.name, "error": str(exc),
                 "parts": [], "total": 0, "unparsed": 0, "roundCount": 0,
                 "yarn": "", "hook": "", "stitchTypes": ""}
            )
    return results
