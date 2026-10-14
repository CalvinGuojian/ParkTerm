"""Convert ai_tools_report.html (2 slides) into a 2-page PPT file."""
import os, re, subprocess, tempfile
from pptx import Presentation
from pptx.util import Inches

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "ai_tools_report.html")
PPT_FILE = os.path.join(BASE_DIR, "ai_tools_report.pptx")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 1. Read the HTML
with open(HTML_FILE, encoding="utf-8") as f:
    html = f.read()

# 2. Extract <style>...</style>
style_match = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
css = style_match.group(1) if style_match else ""

# 3. Extract the two slide blocks by splitting on 'class="slide '
parts = html.split('<div class="slide ')
# parts[0] = before first slide
# parts[1] = slide-1 content (from 'slide-1">...' to before next page-label)
# parts[2] = slide-2 content

slide_blocks = []
for i in range(1, min(len(parts), 3)):
    content = parts[i]
    # Find the closing </div> that ends the slide
    # The slide structure: slide div > header, body, footer
    # We need to find the right closing tags
    # Just take everything up to the next page-label or end
    if i == 1:
        # Slide 1 ends before "<!-- ==================== 页面 02"
        end_marker = "<!-- ==================== 页面 02"
        idx = content.find(end_marker)
        if idx > 0:
            content = content[:idx]
    else:
        # Slide 2 ends before "</body>"
        end_marker = "</body>"
        idx = content.find(end_marker)
        if idx > 0:
            content = content[:idx]
    
    slide_html = '<div class="slide ' + content.strip()
    # Remove trailing page-label divs if any
    slide_html = slide_html.rstrip()
    slide_blocks.append(slide_html)

print(f"Found {len(slide_blocks)} slides")
for i, s in enumerate(slide_blocks):
    print(f"  Slide {i+1}: {len(s)} chars, starts with: {s[:80]}...")

# 4. Create temp HTML files and screenshot each
screenshots = []
temp_files = []

for i, slide_html in enumerate(slide_blocks):
    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
{css}
body {{
  margin: 0; padding: 0;
  background: #e8eaf0;
  width: 1280px;
  height: 720px;
  overflow: hidden;
}}
.slide {{
  margin: 0;
  box-shadow: none;
  border-radius: 0;
}}
</style>
</head>
<body>
{slide_html}
</body>
</html>"""
    
    tmp_path = os.path.join(BASE_DIR, f"_tmp_slide_{i+1}.html")
    png_path = os.path.join(BASE_DIR, f"_tmp_slide_{i+1}.png")
    temp_files.extend([tmp_path, png_path])
    
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    
    # Use Edge headless to screenshot
    file_url = f"file:///{tmp_path.replace(os.sep, '/')}"
    cmd_list = [
        EDGE,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,720",
        "--virtual-time-budget=5000",
        f"--screenshot={png_path}",
        f"--user-data-dir={tempfile.gettempdir()}\\edge_headless_{i+1}",
        file_url,
    ]
    print(f"Capturing slide {i+1}...")
    for attempt in range(3):
        result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=60)
        if os.path.exists(png_path) and os.path.getsize(png_path) > 1000:
            break
        print(f"  Retry {attempt+2}...")
        import time; time.sleep(2)
    
    if os.path.exists(png_path):
        size = os.path.getsize(png_path)
        print(f"  OK: {png_path} ({size} bytes)")
        screenshots.append(png_path)
    else:
        print(f"  FAILED: screenshot not created")
        if result.stderr:
            print(f"  stderr: {result.stderr[:300]}")

# 5. Create PPT
if screenshots:
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    
    for png in screenshots:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_picture(png, 0, 0, prs.slide_width, prs.slide_height)
    
    prs.save(PPT_FILE)
    print(f"\nPPT saved: {PPT_FILE}")
else:
    print("No screenshots captured, PPT not created")

# 6. Cleanup temp files
for f in temp_files:
    try:
        os.remove(f)
    except:
        pass

print("Done!")
