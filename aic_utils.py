"""
aic_utils.py — shared utilities for AIC Streamlit pages.
"""
from __future__ import annotations

import base64
import streamlit as st

ACCENT = "#4E106F"



def save_as_button(
    label: str,
    data: bytes,
    filename: str,
    mime: str,
    key: str = "",
    height: int = 44,
) -> None:
    """
    Render a purple 'Save As…' button using the File System Access API.

    On Chrome / Edge the OS native Save-As dialog opens so the user can
    choose the exact save location and filename.  On other browsers (or if
    the API is blocked) it falls back to a regular browser download.

    Parameters
    ----------
    label    : button text (can include emoji)
    data     : raw bytes to save
    filename : suggested filename shown in the dialog
    mime     : MIME type string
    key      : optional unique suffix for the JS function name
    height   : iframe height in pixels — increase if button is clipped
    """
    b64 = base64.b64encode(data).decode()
    ext = ("." + filename.rsplit(".", 1)[-1]) if "." in filename else ""
    fn_name = f"saveAs_{key}" if key else "saveAsFile"

    # Escape special chars so they survive JS string literals
    safe_fn   = filename.replace("\\", "\\\\").replace("'", "\\'")
    safe_mime = mime.replace("'", "\\'")
    safe_ext  = ext.replace("'", "\\'")

    html = f"""<!doctype html>
<html>
<head>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: transparent; display: flex; align-items: center; }}
  .sa-btn {{
    background: {ACCENT};
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 8px 18px;
    font-size: 14px;
    font-family: "Source Sans Pro", sans-serif;
    font-weight: 500;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: background 0.15s;
    white-space: nowrap;
  }}
  .sa-btn:hover  {{ background: #3a0d54; }}
  .sa-btn:active {{ background: #2d0a42; }}
</style>
</head>
<body>
<button class="sa-btn" onclick="{fn_name}()">{label}</button>
<script>
const _b64_{key}  = '{b64}';
const _fn_{key}   = '{safe_fn}';
const _mime_{key} = '{safe_mime}';
const _ext_{key}  = '{safe_ext}';

async function {fn_name}() {{
  const bytes = Uint8Array.from(atob(_b64_{key}), c => c.charCodeAt(0));

  if (window.showSaveFilePicker) {{
    try {{
      const fh = await window.showSaveFilePicker({{
        suggestedName: _fn_{key},
        types: [{{
          description: 'File',
          accept: {{ [_mime_{key}]: [_ext_{key}] }}
        }}]
      }});
      const w = await fh.createWritable();
      await w.write(bytes);
      await w.close();
      return;
    }} catch (e) {{
      if (e.name === 'AbortError') return;   // user hit Cancel — do nothing
      // any other error: fall through to regular download
    }}
  }}

  // Fallback: regular browser download (non-Chrome/Edge or API blocked)
  const blob = new Blob([bytes], {{ type: _mime_{key} }});
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), {{
    href: url, download: _fn_{key}
  }});
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}}
</script>
</body>
</html>"""

    st.components.v1.html(html, height=height)
