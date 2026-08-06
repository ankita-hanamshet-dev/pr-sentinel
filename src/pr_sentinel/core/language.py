"""Three-layer language detection: extension map -> shebang -> keyword scoring.

Pure heuristics, zero third-party dependencies. Covers 30+ languages.

Ambiguous extensions (e.g. ``.h`` -> C / C++ / Objective-C) are resolved by
keyword scoring when file content is available, since the extension alone is not
decisive. Misdetection is not cosmetic: it selects which style rule set gets
injected into every downstream agent prompt.
"""

from __future__ import annotations

FILENAME_MAP: dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "cmakelists.txt": "cmake",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "go.mod": "go",
    "go.sum": "go",
}

EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".rs": "rust",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".pl": "perl",
    ".pm": "perl",
    ".lua": "lua",
    ".r": "r",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".clj": "clojure",
    ".m": "objective-c",
    ".mm": "objective-c",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".md": "markdown",
    ".xml": "xml",
    ".groovy": "groovy",
    ".tf": "terraform",
    ".ini": "ini",
}

# Extensions the extension layer cannot decide on its own: (candidates, default).
AMBIGUOUS_EXTENSIONS: dict[str, tuple[tuple[str, ...], str]] = {
    ".h": (("c", "cpp", "objective-c"), "c"),
}

SHEBANG_MAP: dict[str, str] = {
    "python": "python",
    "node": "javascript",
    "bash": "shell",
    "sh": "shell",
    "zsh": "shell",
    "ruby": "ruby",
    "perl": "perl",
    "php": "php",
    "lua": "lua",
    "pwsh": "powershell",
}

KEYWORD_SETS: dict[str, tuple[str, ...]] = {
    "python": ("def ", "import ", "elif ", "self", "lambda", "None", "print("),
    "javascript": ("function ", "const ", "let ", "=>", "console.log", "undefined"),
    "typescript": ("interface ", "readonly ", "enum ", "namespace ", ": string", ": number"),
    "go": ("func ", "package ", "chan ", "defer ", ":=", "interface{"),
    "java": ("public ", "class ", "void ", "static ", "System.out", "extends "),
    "c": ("#include", "printf", "struct ", "typedef", "malloc", "int main"),
    "cpp": ("std::", "template<", "template <", "namespace ", "cout", "nullptr", "class "),
    "objective-c": ("@interface", "@implementation", "@end", "#import", "nonatomic", "NSString"),
    "ruby": ("def ", "end", "puts ", "require ", "elsif ", "nil", "attr_"),
    "rust": ("fn ", "let mut", "impl ", "pub ", "match ", "println!", "::"),
    "shell": ("echo ", "then", "fi", "esac", "done", "elif "),
    "php": ("<?php", "echo ", "function ", "->", "::", "$"),
}


def _extension(name: str) -> str:
    if "." in name:
        return "." + name.rsplit(".", 1)[1].lower()
    return ""


def _from_shebang(content: str) -> str | None:
    first = content.splitlines()[0] if content else ""
    if not first.startswith("#!"):
        return None
    for interpreter, language in SHEBANG_MAP.items():
        if interpreter in first:
            return language
    return None


def _score(content: str, language: str) -> int:
    return sum(content.count(keyword) for keyword in KEYWORD_SETS.get(language, ()))


def _from_keywords(content: str, candidates: tuple[str, ...] | None = None) -> str | None:
    langs = candidates if candidates is not None else tuple(KEYWORD_SETS)
    best_lang: str | None = None
    best_score = 0
    for language in langs:
        score = _score(content, language)
        if score > best_score:
            best_score = score
            best_lang = language
    return best_lang


def detect_language_verbose(path: str, content: str | None = None) -> tuple[str, str]:
    """Detect a file's language and report which layer resolved it."""
    name = path.rsplit("/", 1)[-1]
    if name.lower() in FILENAME_MAP:
        return FILENAME_MAP[name.lower()], "filename"

    extension = _extension(name)
    if extension in AMBIGUOUS_EXTENSIONS:
        candidates, default = AMBIGUOUS_EXTENSIONS[extension]
        if content:
            resolved = _from_keywords(content, candidates)
            if resolved is not None:
                return resolved, "keyword-ambiguous"
        return default, "extension-default"
    if extension in EXTENSION_MAP:
        return EXTENSION_MAP[extension], "extension"

    if content:
        shebang = _from_shebang(content)
        if shebang is not None:
            return shebang, "shebang"
        keyword = _from_keywords(content)
        if keyword is not None:
            return keyword, "keyword"
    return "unknown", "unknown"


def detect_language(path: str, content: str | None = None) -> str:
    """Detect a file's language via extension, then shebang, then keywords."""
    return detect_language_verbose(path, content)[0]
