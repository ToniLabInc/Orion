"""Writes THIRD_PARTY_NOTICES.md: what the committed HUD build (orion/hud/dist) carries of other people's work, with each
one's licence and copyright line, the licence texts, and the fonts' and the star catalogue's notices.

    uv run --only-group web npm --prefix web ci     # the packages must be installed (their LICENSE files are read)
    python3 web/scripts/third_party_notices.py > THIRD_PARTY_NOTICES.md

Standard library only. It reads the production packages of web/package-lock.json (not the build tools, which the build
doesn't carry) and skips `@types/*` (types only), then each package's own LICENSE file for its copyright line.
"""

import json
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
LOCK = WEB / "package-lock.json"
FONTS = ("@fontsource-variable/geist", "@fontsource-variable/geist-mono", "@fontsource/instrument-serif")
COPYRIGHT = re.compile(r"^[ \t>*#/-]*((?:copyright|\(c\)|©)[^\n]*)", re.IGNORECASE | re.MULTILINE)
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "license", "license.md", "License.md")

TEXTS = {
    "MIT": """Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.""",
    "ISC": """Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby
granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF
THIS SOFTWARE.""",
    "BSD-3-Clause": """Redistribution and use in source and binary forms, with or without modification, are permitted provided that
the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following
   disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following
   disclaimer in the documentation and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote
   products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.""",
    "0BSD": """Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby
granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF
THIS SOFTWARE.""",
    "Unlicense": """This is free and unencumbered software released into the public domain. For more information, see
<https://unlicense.org>.""",
    "Apache-2.0": """Licensed under the Apache License, Version 2.0; you may not use these files except in compliance with the
License. You may obtain a copy at <https://www.apache.org/licenses/LICENSE-2.0>. Unless required by applicable law or
agreed to in writing, software distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied.""",
}

STARS = """Horizon's sky (`web/src/core/stars.bin`, built by `web/scripts/build_stars.py`) is drawn from the Bright Star
Catalogue, 5th Revised Ed. (Preliminary Version): Hoffleit D., Warren Jr W.H., Astronomical Data Center, NSSDC/ADC
(1991), from the CDS, Strasbourg (VizieR catalogue V/50). The star data are facts of the sky; the catalogue is
acknowledged as its authors ask."""


def packages() -> list[tuple[str, str, str, str]]:
    """The production packages: (name, version, SPDX licence, copyright line), sorted by name."""
    lock = json.loads(LOCK.read_text())
    found = []
    for path, meta in lock["packages"].items():
        if not path or meta.get("dev") or meta.get("devOptional"):
            continue
        name = path.split("node_modules/")[-1]
        if name.startswith("@types/") or name in FONTS:
            continue
        found.append((name, meta.get("version", ""), str(meta.get("license", "?")), copyright_line(WEB / path)))
    return sorted(set(found))


def copyright_line(folder: Path) -> str:
    """The first copyright line of a package's licence file, or what to look at when it has none."""
    for file in LICENSE_FILES:
        if (folder / file).is_file():
            match = COPYRIGHT.search((folder / file).read_text(errors="replace"))
            return without_contacts(match.group(1)) if match else "(no copyright line in its licence file)"
    return "(its package ships no licence file: see its repository)"


def without_contacts(line: str) -> str:
    """A copyright line without anyone's e-mail address (the notice is the name and the year; people's addresses don't
    belong in a file that is republished)."""
    line = re.sub(r"<(?:mailto:)?[^<>@\s]+@[^<>\s]+>", "", line)
    line = re.sub(r"\(?\bmailto:\S+\)?|\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "", line)
    return re.sub(r"\s{2,}", " ", line).strip()


def main() -> None:
    """Print the notices."""
    out = [
        "# Third-party notices",
        "",
        "Orion's own code is MIT ([LICENSE](LICENSE)). The built HUD in `orion/hud/dist` carries the work of others, listed",
        "here with their licences. The Python packages Orion installs (`uv.lock`) are not copied into this repository; each",
        "comes with its own licence, and the README's *Licences* section says which are not permissive.",
        "",
        "Generated by `web/scripts/third_party_notices.py` from `web/package-lock.json`: run it again after changing the HUD's",
        "dependencies.",
        "",
        "## Fonts (SIL Open Font License 1.1)",
        "",
        "Geist and Geist Mono (Copyright 2024 The Geist Project Authors, <https://github.com/vercel/geist-font>) and",
        "Instrument Serif (Copyright 2022 The Instrument Serif Project Authors,",
        "<https://github.com/Instrument/instrument-serif>), as the `@fontsource` packages ship them, in",
        "`orion/hud/dist/assets/*.woff2` and `*.woff`. They are used unmodified. This Font Software is licensed under the",
        "SIL Open Font License, Version 1.1, which is copied below and available with a FAQ at <https://openfontlicense.org>.",
        "",
        "```text",
        ofl().rstrip(),
        "```",
        "",
        "## The sky",
        "",
        STARS,
        "",
        "## The HUD's packages",
        "",
        "| Package | Version | Licence | Copyright |",
        "|---|---|---|---|",
    ]
    used: set[str] = set()
    for name, version, licence, notice in packages():
        out.append(f"| {name} | {version} | {licence} | {notice.replace('|', '/')} |")
        used.update(re.split(r"\s+(?:and|AND|OR|or)\s+", licence.strip("() ")))
    out += ["", "## Licence texts", ""]
    for spdx, text in TEXTS.items():
        if spdx in used:
            out += [f"### {spdx}", "", text, ""]
    sys.stdout.write("\n".join(out).rstrip() + "\n")


def ofl() -> str:
    """The OFL's text, from the Geist package's own licence file (everything from its first rule on)."""
    file = WEB / "node_modules" / "@fontsource-variable" / "geist" / "LICENSE"
    text = file.read_text()
    return text[text.index("-----------------------------------------------------------") :]


if __name__ == "__main__":
    main()
