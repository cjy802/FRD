# -*- coding: utf-8 -*-
# 一次性补丁：删除“巡查危险”场景页并清理配套逻辑
import re

F = '市场痛点动画.html'
html = open(F, encoding='utf-8').read()

def cut(s):
    global html
    assert s in html, 'NOT FOUND: ' + s[:70]
    html = html.replace(s, '')

# 1) 注释头：去掉危险场景解说行，更新时长
cut('    [危险]   更矛盾的是：最需要巡查的时刻，恰恰是暴雨中人工巡查最危险的时刻。\n')
cut('约 31.5 秒。建议解说词')
html = html.replace('约 26.5 秒。建议解说词', '约 26.5 秒。建议解说词')
cut('约 33 秒')
html = html.replace('录屏建议录最后约 28 秒', '录屏建议录最后约 28 秒')
html = html.replace('录屏建议录最后约 33 秒', '录屏建议录最后约 28 秒')

# 2) 场景4 整段删除
html, n = re.subn(r'\n  <!-- 场景4 · 巡查危险 -->\n  <section class="scene" data-s="danger">.*?</section>',
                  '', html, count=1, flags=re.S)
assert n == 1, 'danger section'

# 3) JS 时间线：合并后续场景时刻
cut("""    [18600, function(){ sc('danger'); stage.classList.add('heavy'); }],
    [19100, doFlash],
    [20850, doFlash],
    [22600, function(){ sc('sum'); stage.classList.remove('heavy'); }],
    [28400, function(){ sc('outro'); }]
""")
html = html.replace("""    [18400, function(){ sc('sum'); stage.classList.remove('heavy'); }],
    [24200, function(){ sc('outro'); }]
""", """    [18400, function(){ sc('sum'); }],
    [24200, function(){ sc('outro'); }]
""")

# 4) 闪电与暴雨加重逻辑（只为该页存在）
cut("""  function doFlash(){
    flash.classList.remove('go'); void flash.offsetWidth; flash.classList.add('go');
  }
""")
cut("""      fill  = document.getElementById('tfill'),
      flash = document.getElementById('flash');
""")
html = html.replace("      fill  = document.getElementById('tfill'),\n", "      fill  = document.getElementById('tfill');\n")
cut("    stage.classList.remove('heavy');\n")
cut('  <div class="flash" id="flash"></div>\n')
cut('  <div class="rain2" id="rain2"></div>\n')
cut("""  .flash{ position:absolute; inset:0; opacity:0; pointer-events:none;
    background:radial-gradient(900px 500px at 62% 12%, rgba(255,255,255,.95), rgba(255,255,255,0) 60%); }
  .flash.go{ animation:flashK .55s ease-out; }
  @keyframes flashK{ 0%{opacity:0;} 12%{opacity:.8;} 30%{opacity:.12;} 46%{opacity:.5;} 100%{opacity:0;} }

""")
cut("""  .rain2{ opacity:0; transition:opacity 1s; transform:rotate(8deg) scale(1.2); transform-origin:top center; }
  .rain2 i{ background:linear-gradient(to bottom, rgba(185,215,255,0), rgba(185,215,255,.55)); }
  .heavy .rain2{ opacity:.9; }
""")
cut("  mkRain(document.getElementById('rain2'), 95, true);\n")

open(F, 'w', encoding='utf-8').write(html)
print('done, size', len(html))
