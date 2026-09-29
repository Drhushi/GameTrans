"""LZString（**lz-string 1.4.4**，即 RPG Maker MV 发布版里 vendored 的那一份）。

**为什么这属于引擎适配层**：RPG Maker MV 没有官方翻译工具，但它自己那套
``SRD_DataCompressor`` 会把 ``data/*.json`` 压成 LZString-Base64 再放进
``data/compressed/``（真靶 88 个数据文件全部如此，明文已被作者删掉）。要读内容、
要写回内容，就必须能解、能压 —— 这是这个引擎的私有格式。

**为什么不用第三方库**：项目约定零第三方运行时依赖（只用标准库）。

这个实现是照着游戏自带 ``www/js/libs/lz-string.js``（压缩过的 1.4.4）**逐句**还原的。
1.4.4 的 base64 形态与新版 lz-string 完全不同，不能凭记忆写：

* ``compress`` 先把输入压成串"16 位码元"（每位输出字符装 16 个比特），
  ``compressToBase64`` 再把这些码元按 6 比特重新分组写成 base64；
  ``decompressFromBase64`` 反向重建码元串，再交给 ``decompress``。
* 压缩与解压都按 **UTF-16 码元**算（``charAt`` 一次取一个码元），星平面字符算两个 ——
  所以内部分解成码元再跑算法，否则与引擎产出的位流对不上。

**两处刻意偏离 JS 的地方**（都是为了"不许静默吞掉"，见契约 §1.1.2）：

1. JS 的 ``decompressFromBase64`` 会先把字母表外的字符**静默剃掉**
   （``replace(/[^A-Za-z0-9+/=]/g, "")``）。我们不这么做：遇到字母表外的字符直接报错
   —— 被改坏的文件应该指出来，而不是悄悄少掉内容；
2. JS 解不出来时返回 ``null`` / 空串。我们只在"输入本来就是空"时返回空串，
   其余解不出内容的情况一律报错。

**算法归属**：lz-string 由 Pieroxy 开发，**MIT 许可**（https://github.com/pieroxy/lz-string）。
本文件是它的 Python 还原，按 MIT 的要求在此保留原作者与许可的声明。
"""

from __future__ import annotations

__all__ = ["LZStringError", "compress_to_base64", "decompress_from_base64"]

#: lz-string 的 base64 字母表：索引 64 是 ``=``（JS 用 ``charAt(64)`` 当补位）。
KEY_STR_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="

_PAD_INDEX = 64
_BITS_PER_UNIT = 16
_FLUSH_POSITION = 15
_UTF16_RESET = 32768


class LZStringError(ValueError):
    """数据不是合法的 LZString-Base64。"""


# --------------------------------------------------------------------------- #
# UTF-16 码元
# --------------------------------------------------------------------------- #


def _to_units(text: str) -> list[str]:
    """按 UTF-16 码元切分 —— 与 JS 的 ``charAt`` 逐字对齐。"""
    raw = text.encode("utf-16-le", "surrogatepass")
    return [chr(raw[i] | (raw[i + 1] << 8)) for i in range(0, len(raw), 2)]


def _from_units(joined: str) -> str:
    """把由码元拼起来的字符串还原成 Python 文本。"""
    raw = bytearray()
    for char in joined:
        code = ord(char)
        raw.append(code & 0xFF)
        raw.append((code >> 8) & 0xFF)
    return raw.decode("utf-16-le", "surrogatepass")


# --------------------------------------------------------------------------- #
# 压缩
# --------------------------------------------------------------------------- #


def compress_to_base64(text: str) -> str:
    """``LZString.compressToBase64`` 的等价实现。"""
    packed = _compress(_to_units(text))
    units = [ord(char) for char in packed]
    count = len(units)
    out: list[str] = []
    cursor = 0
    while cursor < count * 2:
        if cursor % 2 == 0:
            high = units[cursor // 2] >> 8
            low = units[cursor // 2] & 255
            third = units[cursor // 2 + 1] >> 8 if cursor // 2 + 1 < count else None
        else:
            high = units[(cursor - 1) // 2] & 255
            if (cursor + 1) // 2 < count:
                low = units[(cursor + 1) // 2] >> 8
                third = units[(cursor + 1) // 2] & 255
            else:
                low = third = None
        cursor += 3
        first = high >> 2
        second = ((high & 3) << 4) | (0 if low is None else low >> 4)
        middle = (0 if low is None else (low & 15) << 2) | (0 if third is None else third >> 6)
        last = 0 if third is None else third & 63
        if low is None:
            middle = last = _PAD_INDEX
        elif third is None:
            last = _PAD_INDEX
        out.append(
            KEY_STR_BASE64[first]
            + KEY_STR_BASE64[second]
            + KEY_STR_BASE64[middle]
            + KEY_STR_BASE64[last]
        )
    return "".join(out)


def _compress(units: list[str]) -> str:
    """``LZString.compress``：输出串里每位字符装 16 个比特。"""
    dictionary: dict[str, int] = {}
    to_create: set[str] = set()
    out: list[int] = []
    value = 0
    position = 0
    enlarge_in = 2
    dict_size = 3
    num_bits = 2
    w = ""

    def push(bit: int) -> None:
        nonlocal value, position
        value = ((value << 1) | (bit & 1)) & 0xFFFF
        if position == _FLUSH_POSITION:
            out.append(value)
            value = 0
            position = 0
        else:
            position += 1

    def push_bits(number: int, count: int) -> None:
        for _ in range(count):
            push(number & 1)
            number >>= 1

    def emit_new_entry(entry: str) -> None:
        """``toCreate`` 里的条目：先写"新字符"标记，再写码点。"""
        nonlocal enlarge_in, num_bits
        if ord(entry[0]) < 256:
            for _ in range(num_bits):
                push(0)
            push_bits(ord(entry[0]), 8)
        else:
            marker = 1
            for _ in range(num_bits):
                push(marker)
                marker = 0
            push_bits(ord(entry[0]), 16)
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        to_create.discard(entry)

    def shrink() -> None:
        nonlocal enlarge_in, num_bits
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

    for char in units:
        if char not in dictionary:
            dictionary[char] = dict_size
            dict_size += 1
            to_create.add(char)
        wc = w + char
        if wc in dictionary:
            w = wc
            continue
        if w in to_create:
            emit_new_entry(w)
        else:
            push_bits(dictionary[w], num_bits)
        shrink()
        dictionary[wc] = dict_size
        dict_size += 1
        w = char

    if w:
        if w in to_create:
            emit_new_entry(w)
        else:
            push_bits(dictionary[w], num_bits)
        shrink()

    marker = 2
    for _ in range(num_bits):
        push(marker & 1)
        marker >>= 1
    # 收尾：把当前这一个字符补满
    while True:
        value = (value << 1) & 0xFFFF
        if position == _FLUSH_POSITION:
            out.append(value)
            break
        position += 1
    return "".join(chr(code) for code in out)


# --------------------------------------------------------------------------- #
# 解压
# --------------------------------------------------------------------------- #


def decompress_from_base64(data: str) -> str:
    """``LZString.decompressFromBase64`` 的等价实现。

    字母表外的字符与"非空却解不出内容"都报 :class:`LZStringError`（见模块文档）。
    """
    if not data:
        return ""
    for index, char in enumerate(data):
        if char not in KEY_STR_BASE64:
            raise LZStringError(
                f"第 {index} 个字符 {char!r} 不在 LZString base64 字母表里"
            )

    def at(offset: int) -> int:
        # JS 的 ``e.charAt(c++)`` 越界返回 ""，而 ``indexOf("")`` 是 0
        return KEY_STR_BASE64.index(data[offset]) if offset < len(data) else 0

    units: list[int] = []
    counter = 0
    cursor = 0
    pending = 0
    while cursor < len(data):
        first = at(cursor)
        second = at(cursor + 1)
        third = at(cursor + 2)
        fourth = at(cursor + 3)
        cursor += 4
        low_unit = (first << 2) | (second >> 4)
        high_unit = ((second & 15) << 4) | (third >> 2)
        tail_unit = ((third & 3) << 6) | fourth
        if counter % 2 == 0:
            pending = low_unit << 8
            if third != _PAD_INDEX:
                units.append(pending | high_unit)
            if fourth != _PAD_INDEX:
                pending = tail_unit << 8
        else:
            units.append(pending | low_unit)
            if third != _PAD_INDEX:
                pending = high_unit << 8
            if fourth != _PAD_INDEX:
                units.append(pending | tail_unit)
        counter += 3

    packed = "".join(chr(code & 0xFFFF) for code in units)
    if packed == "":
        raise LZStringError("这份数据里没有可解的内容（只有补位字符）")
    return _from_units(_decompress(packed))


def _decompress(packed: str) -> str:
    """``LZString.decompress``：从 16 位码元串还原文本。"""
    codes = [ord(char) for char in packed]
    dictionary: list[str | None] = [None] * (len(codes) * 4 + 8)
    enlarge_in = 4
    dict_size = 4
    num_bits = 3
    state = {
        "value": codes[0],
        "position": _UTF16_RESET,
        "index": 1,
    }

    def read(count: int) -> int:
        bits = 0
        power = 1
        for _ in range(count):
            resb = state["value"] & state["position"]
            state["position"] >>= 1
            if state["position"] == 0:
                state["position"] = _UTF16_RESET
                index = state["index"]
                state["index"] += 1
                # 越界时 JS 的 charCodeAt 给 NaN，而 ``NaN & x`` 是 0
                state["value"] = codes[index] if index < len(codes) else 0
            if resb > 0:
                bits |= power
            power <<= 1
        return bits

    for index in range(3):
        dictionary[index] = chr(index)

    marker = read(2)
    if marker == 0:
        char = chr(read(8))
    elif marker == 1:
        char = chr(read(16))
    else:
        # marker == 2：空流
        return ""
    dictionary[3] = char
    previous = char
    result = char

    guard = 0
    limit = max(4096, len(codes) * 64)
    while True:
        guard += 1
        if guard > limit:
            raise LZStringError("位流没有终止标记（疑似被截断或损坏）")
        if state["index"] > len(codes):
            raise LZStringError("位流在读到终止标记之前就用完了（疑似被截断）")
        code = read(num_bits)
        if code == 0:
            dictionary[dict_size] = chr(read(8))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 1:
            dictionary[dict_size] = chr(read(16))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 2:
            return result
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        if code < len(dictionary) and dictionary[code] is not None and code > 2:
            entry = dictionary[code]
        elif code == dict_size:
            entry = previous + previous[0]
        else:
            raise LZStringError("位流引用了字典里不存在的条目（数据已损坏）")
        assert entry is not None
        result += entry
        dictionary[dict_size] = previous + entry[0]
        dict_size += 1
        enlarge_in -= 1
        previous = entry
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
