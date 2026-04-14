#!/usr/bin/env python3
"""
cmake_visualizer.py - CMake Project Visualizer
Analyzes CMake projects and generates a self-contained HTML report.

Usage:
    python cmake_visualizer.py <project_root> [--build-dir <path>] [--output <file>] [--open]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from html import escape
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CMakeTarget:
    name: str
    type: str          # executable | static | shared | interface | external
    sources: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    includes: list[dict] = field(default_factory=list)  # {"path": str, "visibility": str}
    defined_in: str = ""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class CMakeParser:

    _SKIP_EXE = {"WIN32", "MACOSX_BUNDLE", "EXCLUDE_FROM_ALL"}
    _SKIP_LIB = {"EXCLUDE_FROM_ALL", "GLOBAL"}
    _LIB_TYPES = {"STATIC", "SHARED", "MODULE", "INTERFACE", "OBJECT"}
    _VISIBILITY = {"PUBLIC", "PRIVATE", "INTERFACE"}
    _LINK_KW = {
        "PUBLIC", "PRIVATE", "INTERFACE",
        "LINK_PUBLIC", "LINK_PRIVATE", "LINK_INTERFACE_LIBRARIES",
        "GENERAL", "DEBUG", "OPTIMIZED", "BEFORE",
    }

    def parse_directory(self, root: Path) -> tuple[dict[str, CMakeTarget], list[Path]]:
        """Parse all CMakeLists.txt files under root and return (targets, cmake_files)."""
        cmake_files = sorted(root.rglob("CMakeLists.txt"))

        # Collect (rel_path, commands) for each file
        file_commands: list[tuple[str, list]] = []
        for cmake_file in cmake_files:
            try:
                content = cmake_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = cmake_file.relative_to(root).as_posix()
            commands = self._extract_commands(content)
            file_commands.append((rel, commands))

        targets: dict[str, CMakeTarget] = {}

        # Pass 1: define targets (add_executable / add_library)
        for rel, commands in file_commands:
            for cmd, args in commands:
                try:
                    lc = cmd.lower()
                    if lc == "add_executable":
                        self._parse_add_executable(args, rel, targets)
                    elif lc == "add_library":
                        self._parse_add_library(args, rel, targets)
                except Exception:
                    pass

        # Pass 2: sources / deps / includes (may reference targets from other files)
        for rel, commands in file_commands:
            for cmd, args in commands:
                try:
                    lc = cmd.lower()
                    if lc == "target_sources":
                        self._parse_target_sources(args, targets)
                    elif lc == "target_link_libraries":
                        self._parse_target_link_libraries(args, targets)
                    elif lc == "target_include_directories":
                        self._parse_target_include_directories(args, targets)
                except Exception:
                    pass

        # Promote external deps (referenced but not defined) to explicit external targets
        ext_names: set[str] = set()
        for t in targets.values():
            ext_names.update(t.deps)
        for dep in ext_names:
            if dep not in targets:
                targets[dep] = CMakeTarget(name=dep, type="external", defined_in="(external)")

        return targets, cmake_files

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_commands(self, content: str) -> list[tuple[str, str]]:
        """Return list of (command_name, args_string) from CMake source text."""
        # Strip line comments, preserving quoted sections
        lines = []
        for line in content.splitlines():
            clean: list[str] = []
            in_quote = False
            for ch in line:
                if ch == '"':
                    in_quote = not in_quote
                    clean.append(ch)
                elif ch == '#' and not in_quote:
                    break
                else:
                    clean.append(ch)
            lines.append("".join(clean))
        content = "\n".join(lines)

        commands: list[tuple[str, str]] = []
        cmd_re = re.compile(r"([A-Za-z_]\w*)\s*\(")
        i = 0
        while i < len(content):
            m = cmd_re.search(content, i)
            if not m:
                break
            cmd_name = m.group(1)
            j = m.end()        # position just after the opening '('
            depth = 1
            while j < len(content) and depth > 0:
                c = content[j]
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                j += 1
            args_str = content[m.end():j - 1]
            commands.append((cmd_name, args_str))
            i = j
        return commands

    def _tokenize(self, args_str: str) -> list[str]:
        """Split CMake argument string into tokens (respects double quotes)."""
        tokens: list[str] = []
        cur: list[str] = []
        in_q = False
        i = 0
        s = args_str.strip()
        while i < len(s):
            ch = s[i]
            if ch == '"':
                in_q = not in_q
                cur.append(ch)
            elif ch == '#' and not in_q:
                # inline comment — skip to EOL
                while i < len(s) and s[i] != '\n':
                    i += 1
                continue
            elif ch in ' \t\n\r' and not in_q:
                if cur:
                    tokens.append("".join(cur))
                    cur = []
            else:
                cur.append(ch)
            i += 1
        if cur:
            tokens.append("".join(cur))
        return tokens

    def _parse_add_executable(self, args: str, rel: str, targets: dict):
        toks = self._tokenize(args)
        if not toks:
            return
        name = toks[0]
        rest = toks[1:]
        if "IMPORTED" in rest[:2] or (rest and rest[0] == "ALIAS"):
            targets.setdefault(name, CMakeTarget(name=name, type="interface", defined_in=rel))
            return
        sources = [t for t in rest if t not in self._SKIP_EXE and t not in {"IMPORTED", "ALIAS"}]
        targets[name] = CMakeTarget(name=name, type="executable", sources=sources, defined_in=rel)

    def _parse_add_library(self, args: str, rel: str, targets: dict):
        toks = self._tokenize(args)
        if not toks:
            return
        name = toks[0]
        rest = toks[1:]
        if len(rest) >= 2 and rest[0] == "ALIAS":
            targets.setdefault(name, CMakeTarget(name=name, type="interface", defined_in=rel))
            return
        if "IMPORTED" in rest[:2]:
            targets.setdefault(name, CMakeTarget(name=name, type="interface", defined_in=rel))
            return
        lib_type = "static"
        sources: list[str] = []
        for tok in rest:
            if tok == "STATIC":
                lib_type = "static"
            elif tok in ("SHARED", "MODULE"):
                lib_type = "shared"
            elif tok == "INTERFACE":
                lib_type = "interface"
            elif tok == "OBJECT":
                lib_type = "static"
            elif tok not in self._SKIP_LIB:
                sources.append(tok)
        targets[name] = CMakeTarget(name=name, type=lib_type, sources=sources, defined_in=rel)

    def _parse_target_sources(self, args: str, targets: dict):
        toks = self._tokenize(args)
        if len(toks) < 2:
            return
        target_name = toks[0]
        if target_name not in targets:
            return
        for tok in toks[1:]:
            if tok not in self._VISIBILITY:
                targets[target_name].sources.append(tok)

    def _parse_target_link_libraries(self, args: str, targets: dict):
        toks = self._tokenize(args)
        if not toks:
            return
        target_name = toks[0]
        if target_name not in targets:
            # Create placeholder so deps can be recorded even for cross-file references
            targets[target_name] = CMakeTarget(name=target_name, type="external")
        for tok in toks[1:]:
            if tok and tok not in self._LINK_KW:
                if tok not in targets[target_name].deps:
                    targets[target_name].deps.append(tok)

    def _parse_target_include_directories(self, args: str, targets: dict):
        toks = self._tokenize(args)
        if not toks:
            return
        target_name = toks[0]
        if target_name not in targets:
            return
        skip = {"SYSTEM", "BEFORE", "AFTER"}
        vis = "PRIVATE"
        for tok in toks[1:]:
            if tok in self._VISIBILITY:
                vis = tok
            elif tok not in skip and tok:
                targets[target_name].includes.append({"path": tok, "visibility": vis})


# ---------------------------------------------------------------------------
# Graph layout (simplified Sugiyama / longest-path layering)
# ---------------------------------------------------------------------------

NODE_W = 130
NODE_H = 36
H_GAP  = 35
V_GAP  = 80
MARGIN = 55


def compute_layout(targets: dict[str, CMakeTarget]) -> tuple[dict, float, float]:
    """Return (positions, svg_width, svg_height).

    positions maps name -> (x, y) of the top-left corner of each node rect.
    High-layer nodes (many dependents, e.g. executables) are placed at the top.
    """
    if not targets:
        return {}, 700, 300

    names = list(targets.keys())

    # layer[n] = longest outgoing dependency path length from n to a leaf
    #   leaf  → layer 0  (drawn at bottom of SVG)
    #   top   → layer N  (drawn at top of SVG, e.g. executables)
    layer: dict[str, int] = {}

    # Fixed-point iteration (handles cycles gracefully via max-iter bound)
    for _ in range(len(names) + 1):
        changed = False
        for name in names:
            deps = [d for d in targets[name].deps if d in targets]
            if not deps:
                if layer.get(name, -1) < 0:
                    layer[name] = 0
                    changed = True
            else:
                max_dep = max((layer.get(d, 0) for d in deps), default=0)
                nl = max_dep + 1
                if layer.get(name, -1) < nl:
                    layer[name] = nl
                    changed = True
        if not changed:
            break

    for name in names:
        layer.setdefault(name, 0)

    max_layer = max(layer.values(), default=0)

    # Group and sort nodes per layer
    layer_groups: dict[int, list[str]] = defaultdict(list)
    for name in names:
        layer_groups[layer[name]].append(name)
    for g in layer_groups.values():
        g.sort()

    # SVG canvas dimensions
    max_in_row = max(len(v) for v in layer_groups.values()) if layer_groups else 1
    svg_w = max(max_in_row * (NODE_W + H_GAP) + 2 * MARGIN, 700)
    svg_h = max((max_layer + 1) * (NODE_H + V_GAP) + 2 * MARGIN, 300)

    positions: dict[str, tuple[float, float]] = {}
    for lyr, nodes in layer_groups.items():
        # High layer → small y (near top); layer 0 → near bottom
        y_row = max_layer - lyr
        y = MARGIN + y_row * (NODE_H + V_GAP)
        n = len(nodes)
        row_w = n * NODE_W + (n - 1) * H_GAP
        x_start = (svg_w - row_w) / 2
        for i, name in enumerate(nodes):
            x = x_start + i * (NODE_W + H_GAP)
            positions[name] = (x, y)

    return positions, svg_w, svg_h


# ---------------------------------------------------------------------------
# Colours / labels
# ---------------------------------------------------------------------------

TYPE_COLORS = {
    "executable": "#3B82F6",
    "static":     "#22C55E",
    "shared":     "#F97316",
    "interface":  "#8B5CF6",
    "external":   "#94A3B8",
}

TYPE_LABELS = {
    "executable": "Executable",
    "static":     "Static Lib",
    "shared":     "Shared Lib",
    "interface":  "Interface",
    "external":   "External",
}


# ---------------------------------------------------------------------------
# HTML Generator
# ---------------------------------------------------------------------------

class HTMLGenerator:

    def generate(
        self,
        targets: dict[str, CMakeTarget],
        cmake_files: list[Path],
        root: Path,
        build_dir: str,
    ) -> str:
        positions, svg_w, svg_h = compute_layout(targets)

        # Node data for JS (keyed by name)
        node_data: dict[str, dict] = {}
        for name, t in targets.items():
            node_data[name] = {
                "name":       name,
                "type":       t.type,
                "typeLabel":  TYPE_LABELS.get(t.type, t.type),
                "color":      TYPE_COLORS.get(t.type, "#94A3B8"),
                "sources":    t.sources,
                "deps":       t.deps,
                "includes":   t.includes,
                "definedIn":  t.defined_in,
            }

        # Build each section
        svg_str       = self._svg(targets, positions, svg_w, svg_h)
        filetree_html = self._filetree(cmake_files, root)
        sources_html  = self._sources(targets)
        includes_html = self._includes(targets)
        outputs_html  = self._outputs(targets, root, build_dir)

        target_count = sum(1 for t in targets.values() if t.type != "external")
        ext_count    = sum(1 for t in targets.values() if t.type == "external")

        return _render_html(
            project_root    = escape(str(root)),
            target_count    = target_count,
            ext_count       = ext_count,
            cmake_file_count= len(cmake_files),
            svg_content     = svg_str,
            filetree_html   = filetree_html,
            sources_html    = sources_html,
            includes_html   = includes_html,
            outputs_html    = outputs_html,
            node_data_json  = json.dumps(node_data, ensure_ascii=False),
        )

    # ------------------------------------------------------------------
    # SVG graph
    # ------------------------------------------------------------------

    def _svg(
        self,
        targets: dict,
        positions: dict,
        svg_w: float,
        svg_h: float,
    ) -> str:
        if not targets:
            return (
                f'<svg viewBox="0 0 700 200" width="100%">'
                f'<text x="350" y="100" text-anchor="middle" fill="#64748B" '
                f'font-family="sans-serif" font-size="16">No targets found.</text></svg>'
            )

        parts: list[str] = []
        parts.append(
            f'<svg id="dep-graph" viewBox="0 0 {svg_w:.0f} {svg_h:.0f}" '
            f'width="100%" preserveAspectRatio="xMidYMid meet" '
            f'style="display:block;height:520px;min-height:240px;cursor:grab;user-select:none">'
        )

        # Defs: arrowhead + drop-shadow filter
        parts.append(
            '<defs>'
            '<marker id="arr" markerWidth="9" markerHeight="7" refX="9" refY="3.5" orient="auto">'
            '<polygon points="0 0, 9 3.5, 0 7" fill="#94A3B8"/>'
            '</marker>'
            '<filter id="sh" x="-20%" y="-30%" width="140%" height="160%">'
            '<feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#00000025"/>'
            '</filter>'
            '</defs>'
        )

        # --- Edges (draw before nodes so nodes sit on top) ---
        for name, target in targets.items():
            if name not in positions:
                continue
            x1, y1 = positions[name]
            src_cx = x1 + NODE_W / 2
            src_bot = y1 + NODE_H          # bottom-centre of source node

            for dep in target.deps:
                if dep not in positions:
                    continue
                x2, y2 = positions[dep]
                dst_cx = x2 + NODE_W / 2
                dst_top = y2                # top-centre of destination node

                dy = dst_top - src_bot
                if abs(dy) < 4:
                    # Same layer: arc above
                    mid_x = (src_cx + dst_cx) / 2
                    arc_y = y1 - 45
                    d = (f"M {src_cx:.1f} {y1:.1f} "
                         f"Q {mid_x:.1f} {arc_y:.1f} {dst_cx:.1f} {dst_top:.1f}")
                else:
                    ctrl = abs(dy) * 0.4
                    d = (f"M {src_cx:.1f} {src_bot:.1f} "
                         f"C {src_cx:.1f} {src_bot + ctrl:.1f} "
                         f"{dst_cx:.1f} {dst_top - ctrl:.1f} "
                         f"{dst_cx:.1f} {dst_top:.1f}")

                parts.append(
                    f'<path d="{d}" stroke="#CBD5E1" stroke-width="1.8" '
                    f'fill="none" marker-end="url(#arr)"/>'
                )

        # --- Nodes ---
        for name, target in targets.items():
            if name not in positions:
                continue
            x, y = positions[name]
            color = TYPE_COLORS.get(target.type, "#94A3B8")
            display = name if len(name) <= 17 else name[:15] + "…"
            safe_name   = escape(name)
            safe_display = escape(display)
            cx = x + NODE_W / 2
            cy = y + NODE_H / 2

            parts.append(
                f'<g class="node" data-name="{safe_name}" style="cursor:pointer">'
            )
            # Shadow rect
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{NODE_W}" height="{NODE_H}" '
                f'rx="7" fill="{color}" filter="url(#sh)"/>'
            )
            # Main rect (identical; shadow filter handles the shadow)
            # Type badge strip on left
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="5" height="{NODE_H}" '
                f'rx="7" fill="rgba(0,0,0,0.18)"/>'
                f'<rect x="{x:.1f}" y="{y:.1f}" width="5" height="{NODE_H}" '
                f'fill="rgba(0,0,0,0.18)"/>'
            )
            # Label
            parts.append(
                f'<text x="{cx:.1f}" y="{cy:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'fill="white" font-family="ui-monospace,monospace" '
                f'font-size="11" font-weight="700">'
                f'{safe_display}</text>'
            )
            parts.append('</g>')

        # --- Legend ---
        lx, ly = 10, 10
        legend_h = len(TYPE_COLORS) * 18 + 16
        parts.append(
            f'<rect x="{lx}" y="{ly}" width="148" height="{legend_h}" '
            f'rx="7" fill="white" stroke="#E2E8F0" stroke-width="1" opacity="0.93"/>'
        )
        for i, (t, col) in enumerate(TYPE_COLORS.items()):
            iy = ly + 10 + i * 18
            parts.append(
                f'<rect x="{lx+8}" y="{iy}" width="12" height="12" rx="3" fill="{col}"/>'
                f'<text x="{lx+26}" y="{iy+10}" font-family="sans-serif" '
                f'font-size="11" fill="#475569">{TYPE_LABELS[t]}</text>'
            )

        parts.append('</svg>')
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # File tree tab
    # ------------------------------------------------------------------

    def _filetree(self, cmake_files: list[Path], root: Path) -> str:
        if not cmake_files:
            return '<p class="empty">No CMakeLists.txt files found.</p>'

        # Collect all ancestor directories
        all_dirs: set[Path] = set()
        for f in cmake_files:
            rel = f.relative_to(root)
            p = rel.parent
            while p != Path("."):
                all_dirs.add(p)
                p = p.parent

        parts: list[str] = ['<div class="filetree"><ul class="tree-root">']

        def render_dir(parent: Path, depth: int) -> None:
            indent_px = depth * 20
            # Sub-directories of this parent
            subdirs = sorted(d for d in all_dirs if d.parent == parent)
            # CMakeLists.txt files directly in this parent
            local_files = [f for f in cmake_files if f.relative_to(root).parent == parent]

            for d in subdirs:
                parts.append(
                    f'<li class="tree-dir" style="padding-left:{indent_px}px">'
                    f'<span class="icon-dir">&#128193;</span>'
                    f'<span class="dir-name">{escape(d.name)}/</span>'
                    f'<ul>'
                )
                render_dir(d, depth + 1)
                parts.append('</ul></li>')

            for f in local_files:
                rel_str = f.relative_to(root).as_posix()
                parts.append(
                    f'<li class="tree-file" style="padding-left:{indent_px}px">'
                    f'<span class="icon-cmake">&#128196;</span>'
                    f'<span class="cmake-name">CMakeLists.txt</span>'
                    f'<span class="file-rel">&#x2014; {escape(rel_str)}</span>'
                    f'</li>'
                )

        render_dir(Path("."), 0)
        parts.append('</ul></div>')
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Source mapping tab
    # ------------------------------------------------------------------

    def _sources(self, targets: dict) -> str:
        non_ext = {n: t for n, t in sorted(targets.items()) if t.type != "external"}
        if not non_ext:
            return '<p class="empty">No targets found.</p>'

        parts = ['<div class="cards">']
        for name, t in non_ext.items():
            col   = TYPE_COLORS.get(t.type, "#94A3B8")
            label = TYPE_LABELS.get(t.type, t.type)
            parts.append(
                f'<div class="card">'
                f'<div class="card-hdr" style="border-left:4px solid {col}">'
                f'<span class="card-title">{escape(name)}</span>'
                f'<span class="badge" style="background:{col}">{label}</span>'
                f'</div>'
            )
            if t.sources:
                parts.append('<ul class="src-list">')
                for s in t.sources:
                    parts.append(f'<li><code>{escape(s)}</code></li>')
                parts.append('</ul>')
            else:
                parts.append('<p class="no-src">No source files listed.</p>')
            parts.append('</div>')
        parts.append('</div>')
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Include paths tab
    # ------------------------------------------------------------------

    def _includes(self, targets: dict) -> str:
        with_inc = {n: t for n, t in sorted(targets.items()) if t.includes}
        if not with_inc:
            return '<p class="empty">No target_include_directories entries found.</p>'

        parts = ['<div class="cards">']
        for name, t in with_inc.items():
            col   = TYPE_COLORS.get(t.type, "#94A3B8")
            label = TYPE_LABELS.get(t.type, t.type)
            parts.append(
                f'<div class="card">'
                f'<div class="card-hdr" style="border-left:4px solid {col}">'
                f'<span class="card-title">{escape(name)}</span>'
                f'<span class="badge" style="background:{col}">{label}</span>'
                f'</div>'
                f'<table class="inc-tbl"><thead><tr><th>Visibility</th><th>Path</th></tr></thead><tbody>'
            )
            for inc in t.includes:
                vis = inc.get("visibility", "PRIVATE")
                path = inc.get("path", "")
                parts.append(
                    f'<tr>'
                    f'<td><span class="vis vis-{vis.lower()}">{vis}</span></td>'
                    f'<td><code>{escape(path)}</code></td>'
                    f'</tr>'
                )
            parts.append('</tbody></table></div>')
        parts.append('</div>')
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Build outputs tab
    # ------------------------------------------------------------------

    def _outputs(self, targets: dict, root: Path, build_dir: str) -> str:
        non_ext = {n: t for n, t in sorted(targets.items()) if t.type != "external"}
        if not non_ext:
            return '<p class="empty">No targets found.</p>'

        def estimate(t: CMakeTarget) -> str:
            sub = ""
            if t.defined_in and t.defined_in != "(external)":
                parent = Path(t.defined_in).parent
                if parent != Path("."):
                    sub = parent.as_posix()
            prefix = f"{build_dir}/{sub}" if sub else build_dir
            if t.type == "executable":
                return f"{prefix}/{t.name}"
            elif t.type == "static":
                return f"{prefix}/lib{t.name}.a"
            elif t.type == "shared":
                return f"{prefix}/lib{t.name}.so"
            else:
                return f"{prefix}/{t.name}  (interface / header-only)"

        parts = ['<div class="cards">']
        for name, t in non_ext.items():
            col   = TYPE_COLORS.get(t.type, "#94A3B8")
            label = TYPE_LABELS.get(t.type, t.type)
            out   = estimate(t)
            parts.append(
                f'<div class="card output-card">'
                f'<div class="card-hdr" style="border-left:4px solid {col}">'
                f'<span class="card-title">{escape(name)}</span>'
                f'<span class="badge" style="background:{col}">{label}</span>'
                f'</div>'
                f'<div class="out-path"><code>{escape(out)}</code></div>'
            )
            if t.defined_in:
                parts.append(f'<div class="defined-in">Defined in: <code>{escape(t.defined_in)}</code></div>')
            parts.append('</div>')
        parts.append('</div>')
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# HTML template renderer
# ---------------------------------------------------------------------------

def _render_html(
    project_root: str,
    target_count: int,
    ext_count: int,
    cmake_file_count: int,
    svg_content: str,
    filetree_html: str,
    sources_html: str,
    includes_html: str,
    outputs_html: str,
    node_data_json: str,
) -> str:
    css = r"""
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-sans-serif, system-ui, sans-serif; background: #F8FAFC; color: #1E293B; }

/* ---- header ---- */
header { background: #1E293B; color: white; padding: 20px 28px 16px; }
header h1 { font-size: 22px; font-weight: 700; letter-spacing: -.3px; }
.subtitle { margin-top: 4px; font-size: 13px; color: #94A3B8; }
.subtitle code { background: #334155; padding: 1px 5px; border-radius: 3px; color: #CBD5E1; }
.stats { display: flex; gap: 16px; margin-top: 10px; }
.stat { background: #334155; padding: 3px 10px; border-radius: 12px; font-size: 12px; color: #94A3B8; }

/* ---- tab nav ---- */
.tab-nav { display: flex; gap: 0; background: #F1F5F9; border-bottom: 1px solid #E2E8F0; padding: 0 20px; }
.tab-btn {
  padding: 11px 18px; font-size: 13px; font-weight: 500; border: none;
  background: transparent; cursor: pointer; color: #64748B;
  border-bottom: 2px solid transparent; margin-bottom: -1px; transition: all .15s;
}
.tab-btn:hover { color: #1E293B; }
.tab-btn.active { color: #3B82F6; border-bottom-color: #3B82F6; }

/* ---- content ---- */
main { padding: 20px 24px; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.empty { color: #94A3B8; font-style: italic; padding: 20px 0; }

/* ---- graph toolbar ---- */
.graph-toolbar {
  display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap;
}
.graph-toolbar button {
  padding: 5px 16px; border: 1px solid #CBD5E1; border-radius: 7px;
  background: white; cursor: pointer; font-size: 16px; color: #334155;
  font-weight: 700; line-height: 1.3; transition: background .1s, border-color .1s, box-shadow .1s;
  box-shadow: 0 1px 2px #0000000d;
}
.graph-toolbar button:hover { background: #F1F5F9; border-color: #94A3B8; }
.graph-toolbar button:active { background: #E2E8F0; }
.graph-toolbar .btn-reset { font-size: 12px; padding: 6px 14px; }
.graph-hint { font-size: 12px; color: #94A3B8; margin-left: 6px; }
.zoom-sep { width: 1px; height: 22px; background: #E2E8F0; margin: 0 4px; }

/* ---- graph tab ---- */
.graph-wrap {
  background: white; border: 1px solid #E2E8F0; border-radius: 10px;
  padding: 12px; overflow: hidden;
}
.node rect { transition: opacity .15s; }
.node:hover rect { opacity: .85; }

#node-detail {
  margin-top: 16px; background: white; border: 1px solid #E2E8F0;
  border-radius: 10px; padding: 18px 20px; position: relative;
}
#node-detail h3 { font-size: 16px; font-weight: 700; margin-bottom: 12px; font-family: ui-monospace, monospace; }
#node-detail p { margin: 8px 0 4px; font-size: 13px; }
#node-detail ul { margin: 4px 0 8px 16px; }
#node-detail li { font-size: 13px; margin: 2px 0; }
#node-detail code { background: #F1F5F9; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
.close-btn {
  position: absolute; top: 12px; right: 14px; background: none; border: none;
  font-size: 20px; cursor: pointer; color: #94A3B8; line-height: 1;
}
.close-btn:hover { color: #1E293B; }
.hidden { display: none !important; }

/* ---- file tree ---- */
.filetree { background: white; border: 1px solid #E2E8F0; border-radius: 10px; padding: 16px 20px; }
.tree-root { list-style: none; }
.tree-root ul { list-style: none; }
.tree-dir, .tree-file { padding: 4px 0; font-size: 13px; }
.tree-dir > ul { margin-top: 2px; }
.dir-name { font-weight: 600; color: #334155; }
.cmake-name { font-weight: 700; color: #3B82F6; font-family: ui-monospace, monospace; }
.file-rel { color: #94A3B8; font-size: 12px; margin-left: 6px; }
.icon-dir, .icon-cmake { margin-right: 5px; }

/* ---- cards ---- */
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.card {
  background: white; border: 1px solid #E2E8F0; border-radius: 10px; overflow: hidden;
}
.card-hdr {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px; background: #F8FAFC;
}
.card-title { font-weight: 700; font-family: ui-monospace, monospace; font-size: 14px; }
.badge {
  font-size: 11px; color: white; padding: 2px 8px; border-radius: 10px; font-weight: 600;
}
.src-list { list-style: none; padding: 10px 14px; }
.src-list li { font-size: 12px; padding: 2px 0; }
.src-list code { background: #F1F5F9; padding: 1px 5px; border-radius: 3px; }
.no-src { padding: 10px 14px; color: #94A3B8; font-size: 13px; font-style: italic; }

/* ---- include table ---- */
.inc-tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.inc-tbl th { background: #F8FAFC; padding: 6px 12px; text-align: left; font-size: 11px; color: #64748B; border-bottom: 1px solid #E2E8F0; }
.inc-tbl td { padding: 5px 12px; border-bottom: 1px solid #F1F5F9; }
.inc-tbl tr:last-child td { border-bottom: none; }
.inc-tbl code { background: #F1F5F9; padding: 1px 5px; border-radius: 3px; }

/* ---- visibility badges ---- */
.vis {
  font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 8px;
  display: inline-block;
}
.vis-public    { background: #DBEAFE; color: #1D4ED8; }
.vis-private   { background: #FEE2E2; color: #B91C1C; }
.vis-interface { background: #F3E8FF; color: #7E22CE; }

/* same for detail panel */
.vis-badge { font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 8px; display: inline-block; }
.vis-badge.vis-public    { background: #DBEAFE; color: #1D4ED8; }
.vis-badge.vis-private   { background: #FEE2E2; color: #B91C1C; }
.vis-badge.vis-interface { background: #F3E8FF; color: #7E22CE; }

/* detail badge inline */
.badge-inline {
  font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 10px;
  background: #E2E8F0; color: #334155; display: inline-block;
}

/* ---- build output cards ---- */
.output-card { }
.out-path { padding: 10px 14px; }
.out-path code { background: #F1F5F9; padding: 3px 8px; border-radius: 4px; font-size: 12px; word-break: break-all; }
.defined-in { padding: 0 14px 10px; font-size: 12px; color: #64748B; }
.defined-in code { background: #F1F5F9; padding: 1px 5px; border-radius: 3px; }
"""

    js = r"""
const NODE_DATA = __NODE_DATA_JSON__;

// Tab switching
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// Node click: delegation on SVG (drag-aware — see pan/zoom section below)
const svg = document.getElementById('dep-graph');
if (svg) {
  svg.addEventListener('click', e => {
    if (window._pzHasDragged && window._pzHasDragged()) return; // ignore drag-release
    const g = e.target.closest('.node');
    if (!g) return;
    selectNode(g.dataset.name);
  });
}

function selectNode(name) {
  const data = NODE_DATA[name];
  if (!data) return;

  // Dim all, highlight selected
  document.querySelectorAll('#dep-graph .node rect').forEach(r => {
    r.style.opacity = '0.35';
  });
  document.querySelectorAll('#dep-graph .node').forEach(n => {
    if (n.dataset.name === name) {
      n.querySelectorAll('rect').forEach(r => r.style.opacity = '1');
    }
  });

  // Populate detail panel
  document.getElementById('detail-name').textContent = name;

  const color = data.color || '#94A3B8';
  let html = '';
  html += `<p><strong>Type:</strong> <span class="badge-inline" style="background:${color};color:white">${data.typeLabel}</span></p>`;
  if (data.definedIn) {
    html += `<p><strong>Defined in:</strong> <code>${escHtml(data.definedIn)}</code></p>`;
  }
  if (data.sources && data.sources.length > 0) {
    html += `<p><strong>Sources (${data.sources.length}):</strong></p><ul>`;
    data.sources.forEach(s => { html += `<li><code>${escHtml(s)}</code></li>`; });
    html += '</ul>';
  }
  if (data.deps && data.deps.length > 0) {
    html += `<p><strong>Dependencies (${data.deps.length}):</strong></p><ul>`;
    data.deps.forEach(d => { html += `<li><code>${escHtml(d)}</code></li>`; });
    html += '</ul>';
  }
  if (data.includes && data.includes.length > 0) {
    html += `<p><strong>Include paths (${data.includes.length}):</strong></p><ul>`;
    data.includes.forEach(inc => {
      const vis = inc.visibility || 'PRIVATE';
      html += `<li><span class="vis-badge vis-${vis.toLowerCase()}">${vis}</span> <code>${escHtml(inc.path)}</code></li>`;
    });
    html += '</ul>';
  }

  document.getElementById('detail-body').innerHTML = html;
  document.getElementById('node-detail').classList.remove('hidden');
}

function closeDetail() {
  document.querySelectorAll('#dep-graph .node rect').forEach(r => {
    r.style.opacity = '1';
  });
  document.getElementById('node-detail').classList.add('hidden');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Pan / Zoom for the dependency graph SVG
// ---------------------------------------------------------------------------
(function () {
  const svg = document.getElementById('dep-graph');
  if (!svg) return;

  // Capture original viewBox for reset
  const origVB = svg.getAttribute('viewBox').split(' ').map(Number);
  let [vbX, vbY, vbW, vbH] = origVB;

  function applyVB() {
    svg.setAttribute('viewBox', `${vbX} ${vbY} ${vbW} ${vbH}`);
  }

  // Convert a screen point to SVG user-space coordinates,
  // respecting the current viewBox + preserveAspectRatio transform.
  function screenToSVG(clientX, clientY) {
    const pt = svg.createSVGPoint();
    pt.x = clientX;
    pt.y = clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse());
  }

  // Zoom around an SVG-space anchor point by `factor` (>1 = zoom out, <1 = zoom in)
  function zoomAround(svgX, svgY, factor) {
    vbX = svgX - (svgX - vbX) * factor;
    vbY = svgY - (svgY - vbY) * factor;
    vbW *= factor;
    vbH *= factor;
    applyVB();
  }

  // Zoom around the viewport centre (used by buttons)
  function zoomCenter(factor) {
    zoomAround(vbX + vbW / 2, vbY + vbH / 2, factor);
  }

  // ---- Wheel zoom ----
  svg.addEventListener('wheel', e => {
    e.preventDefault();
    const anchor = screenToSVG(e.clientX, e.clientY);
    const factor = e.deltaY > 0 ? 1.12 : 1 / 1.12;
    zoomAround(anchor.x, anchor.y, factor);
  }, { passive: false });

  // ---- Mouse pan ----
  let isPanning  = false;
  let _dragged   = false;         // true if mouse moved during this press
  let panOrigin  = null;          // SVG-space point where drag started
  let vbAtStart  = null;          // viewBox snapshot at drag start
  let ctmInvAtStart = null;       // CTM inverse captured at mousedown

  svg.addEventListener('mousedown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    isPanning       = true;
    _dragged        = false;
    ctmInvAtStart   = svg.getScreenCTM().inverse();
    const pt        = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    panOrigin       = pt.matrixTransform(ctmInvAtStart);
    vbAtStart       = { x: vbX, y: vbY };
    svg.style.cursor = 'grabbing';
  });

  window.addEventListener('mousemove', e => {
    if (!isPanning) return;
    // Compute current cursor in the coordinate space captured at mousedown.
    // Using the frozen CTM means the pan is perfectly stable even as viewBox changes.
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const cur = pt.matrixTransform(ctmInvAtStart);
    const dx = cur.x - panOrigin.x;
    const dy = cur.y - panOrigin.y;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) _dragged = true;
    vbX = vbAtStart.x - dx;
    vbY = vbAtStart.y - dy;
    applyVB();
  });

  window.addEventListener('mouseup', () => {
    if (!isPanning) return;
    isPanning = false;
    svg.style.cursor = 'grab';
  });

  // Expose drag state so the node-click handler can ignore drag-releases
  window._pzHasDragged = () => _dragged;

  // ---- Toolbar buttons ----
  document.getElementById('btn-zoom-in')   .addEventListener('click', () => zoomCenter(1 / 1.3));
  document.getElementById('btn-zoom-out')  .addEventListener('click', () => zoomCenter(1.3));
  document.getElementById('btn-zoom-reset').addEventListener('click', () => {
    [vbX, vbY, vbW, vbH] = origVB;
    applyVB();
  });

  // ---- Keyboard shortcuts (Ctrl++  Ctrl+-  Ctrl+0) ----
  window.addEventListener('keydown', e => {
    if (!e.ctrlKey && !e.metaKey) return;
    if (e.key === '=' || e.key === '+') { e.preventDefault(); zoomCenter(1 / 1.3); }
    if (e.key === '-')                  { e.preventDefault(); zoomCenter(1.3); }
    if (e.key === '0')                  { e.preventDefault(); [vbX, vbY, vbW, vbH] = origVB; applyVB(); }
  });
})();
"""

    # Inject node data into JS
    js = js.replace("__NODE_DATA_JSON__", node_data_json)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CMake Report \u2014 {project_root}</title>
<style>{css}</style>
</head>
<body>

<header>
  <h1>CMake Project Report</h1>
  <p class="subtitle">Root: <code>{project_root}</code></p>
  <div class="stats">
    <span class="stat">{target_count} target(s)</span>
    <span class="stat">{ext_count} external dep(s)</span>
    <span class="stat">{cmake_file_count} CMakeLists.txt</span>
  </div>
</header>

<nav class="tab-nav">
  <button class="tab-btn active" data-tab="graph">Dependency Graph</button>
  <button class="tab-btn" data-tab="filetree">File Tree</button>
  <button class="tab-btn" data-tab="sources">Source Mapping</button>
  <button class="tab-btn" data-tab="includes">Include Paths</button>
  <button class="tab-btn" data-tab="outputs">Build Outputs</button>
</nav>

<main>
  <div id="tab-graph" class="tab-content active">
    <div class="graph-toolbar">
      <button id="btn-zoom-in"  title="拡大 (Ctrl++)">&#xFF0B;</button>
      <button id="btn-zoom-out" title="縮小 (Ctrl+-)">&#xFF0D;</button>
      <div class="zoom-sep"></div>
      <button id="btn-zoom-reset" class="btn-reset" title="表示をリセット">100%</button>
      <span class="graph-hint">ホイールで拡大縮小 &nbsp;&#x2022;&nbsp; ドラッグで移動</span>
    </div>
    <div class="graph-wrap">
      {svg_content}
    </div>
    <div id="node-detail" class="hidden">
      <button class="close-btn" onclick="closeDetail()">&#x2715;</button>
      <h3 id="detail-name"></h3>
      <div id="detail-body"></div>
    </div>
  </div>

  <div id="tab-filetree" class="tab-content">
    {filetree_html}
  </div>

  <div id="tab-sources" class="tab-content">
    {sources_html}
  </div>

  <div id="tab-includes" class="tab-content">
    {includes_html}
  </div>

  <div id="tab-outputs" class="tab-content">
    {outputs_html}
  </div>
</main>

<script>{js}</script>
</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze a CMake project and generate a visual HTML report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project_root", help="Root directory of the CMake project")
    parser.add_argument(
        "--build-dir",
        default=None,
        metavar="PATH",
        help="Build directory path (default: <project_root>/build)",
    )
    parser.add_argument(
        "--output",
        default="cmake_report.html",
        metavar="FILE",
        help="Output HTML file name (default: cmake_report.html)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="Open the report in a browser after generation",
    )
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        sys.exit(1)
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    build_dir = args.build_dir if args.build_dir else str(root / "build")

    print(f"Scanning {root} ...")
    cmake_parser = CMakeParser()
    targets, cmake_files = cmake_parser.parse_directory(root)

    if not cmake_files:
        print("Warning: no CMakeLists.txt files found.", file=sys.stderr)

    print(
        f"Found {len(cmake_files)} CMakeLists.txt file(s), "
        f"{sum(1 for t in targets.values() if t.type != 'external')} target(s), "
        f"{sum(1 for t in targets.values() if t.type == 'external')} external dep(s)."
    )

    generator = HTMLGenerator()
    html = generator.generate(targets, cmake_files, root, build_dir)

    out_path = Path(args.output)
    out_path.write_text(html, encoding="utf-8")
    print(f"Report written to: {out_path.resolve()}")

    if args.open_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
