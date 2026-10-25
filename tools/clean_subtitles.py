# -*- coding: utf-8 -*-
"""清洗 OCR 字幕:相邻相似条目分组,组内逐字多数投票取最可信文本。"""
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

SRT = Path(r"D:\低空经济\视频\完整视频_10.25_1.0.srt")
NOISE = re.compile(r"[，。、．\.——\-—~“”\"'‘’!?！？:：;；·…（）\(\)\[\]【】\s]")


def norm(text: str) -> str:
    return NOISE.sub("", text)


def parse_srt(path: Path):
    blocks, current = [], {}
    for line in path.read_text(encoding="utf-8").splitlines() + [""]:
        line = line.strip()
        if not line:
            if "start" in current:
                blocks.append(current)
            current = {}
        elif "-->" in line:
            current["start"], current["end"] = [t.strip() for t in line.split("-->")]
        elif line.isdigit() and "start" not in current:
            continue
        else:
            current["text"] = (current.get("text", "") + " " + line).strip()
    return [(b["start"], b["end"], b["text"]) for b in blocks]


def majority_text(variants: list[str]) -> str:
    variants = sorted(variants, key=len, reverse=True)
    out = []
    pos = 0
    while True:
        chars = [v[pos] for v in variants if pos < len(v)]
        if len(chars) < len(variants) / 2:
            break
        char, count = Counter(chars).most_common(1)[0]
        if count <= len(variants) / 2:
            break
        out.append(char)
        pos += 1
    result = "".join(out)
    if len(result) < max(len(v) for v in variants) * 0.5:
        return Counter(variants).most_common(1)[0][0]
    return result


def main() -> None:
    blocks = parse_srt(SRT)
    groups: list[list] = []
    for block in blocks:
        if groups:
            last_norm = norm(groups[-1][-1][2])
            this_norm = norm(block[2])
            if last_norm and this_norm:
                ratio = SequenceMatcher(None, last_norm, this_norm).ratio()
                if ratio >= 0.7 or this_norm in last_norm or last_norm in this_norm:
                    groups[-1].append(block)
                    continue
        groups.append([block])

    out_srt, out_txt = [], []
    for i, group in enumerate(groups, 1):
        variants = [norm(b[2]) for b in group if norm(b[2])]
        text = majority_text(variants) if variants else group[0][2]
        out_srt.append(f"{i}\n{group[0][0]} --> {group[-1][1]}\n{text}\n")
        out_txt.append(text)
    SRT.write_text("\n".join(out_srt), encoding="utf-8")
    SRT.with_name(SRT.stem.replace(".srt", "") + "_字幕.txt").write_text(
        "\n".join(out_txt) + "\n", encoding="utf-8")
    print(f"{len(blocks)} raw -> {len(groups)} cleaned lines")


if __name__ == "__main__":
    main()
