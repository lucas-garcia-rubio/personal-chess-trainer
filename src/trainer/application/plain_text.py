"""Normalize only newly generated teaching, before it enters the durable log."""
import re


class PlainText:
    """Emit safe prefixes; hold ambiguous Markdown until its line is complete.

    Ordinary prose retains token streaming. Buffering ambiguous suffixes avoids
    publishing syntax that a later token would require us to retract. SAN's #,
    +, !, ?, castling and move numbers are never inline formatting delimiters.
    """

    def __init__(self) -> None:
        self._pending = ''
        self._prefix = ''
        self._line_start = True

    def feed(self, text: str) -> str:
        output: list[str] = []
        for char in text:
            if char == '\n':
                line = self._normalize(self._prefix + self._pending)
                if line is not None:
                    output.append(line[len(self._prefix):] + '\n')
                self._pending = ''
                self._prefix = ''
                self._line_start = True
            elif self._pending or char in '!*_`[|~\\' or (
                self._line_start and (char.isspace() or char in '#>+-=' or char.isdigit())
            ):
                self._pending += char
            else:
                output.append(char)
                self._prefix += char
                self._line_start = False
        return ''.join(output)

    def finish(self) -> str:
        result = (self._normalize(self._prefix + self._pending) or '')[len(self._prefix):]
        self._pending = ''
        return result

    @staticmethod
    def _normalize(text: str) -> str | None:
        if re.fullmatch(r'\s*(?:[-*_]\s*){3,}|\s*={3,}\s*', text):
            return None
        if re.fullmatch(r'\s*(?:`{3,}|~{3,}).*', text):
            return None
        if re.fullmatch(r'\s*[| :\-]+\s*', text) and '---' in text:
            return None
        if '|' in text:
            text = ' — '.join(cell.strip() for cell in text.strip().strip('|').split('|'))
        # Numbered prose lists are formatting; SAN move numbers are content.
        text = re.sub(
            r'^\s*\d+[.)]\s+(?![KQRBN]?[a-h]?[1-8]?x?[a-h][1-8]|O-O|0-0)',
            '', text,
        )
        text = re.sub(r'!?\[([^]]*)\]\(([^)]*)\)', r'\1 (\2)', text)
        if re.match(r'^\s{0,3}#{1,6}\s+', text):
            text = re.sub(r'^\s{0,3}#{1,6}\s+', '', text)
            text = re.sub(r'\s+#+\s*$', '', text)
        text = re.sub(r'^\s*>\s?', '', text)
        text = re.sub(r'^\s*[-+*]\s+', '', text)
        text = re.sub(r'^\[[ xX]\]\s+', '', text)
        text = re.sub(r'(`+)(.*?)\1', r'\2', text)
        for marker in ('**', '__', '~~', '*', '_'):
            escaped = re.escape(marker)
            text = re.sub(r'(?<!\w)' + escaped + r'(.+?)' + escaped + r'(?!\w)', r'\1', text)
        # An unfinished emphasis/code delimiter must not leak at termination.
        text = re.sub(r'(?<!\w)[*_`~]+(?=\w)|(?<=\w)[*_`~]+(?!\w)', '', text)
        if re.fullmatch(r'[*_`~]{2,}|#+', text):
            return ''
        return text
