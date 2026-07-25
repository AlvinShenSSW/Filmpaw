# UI/UX 打磨验收 (issue #20)

## 设计系统来源
`python skill/ui-ux-pro-max/scripts/search.py "Windows desktop media archive productivity tool" --design-system -p "Filmpaw"`

采纳其**方法论**(productivity-tool profile): 单一 typographic scale、8px 间距网格、
按钮不溢出规范、150–300ms hover 过渡、无障碍检查表(focus-visible / 4.5:1 对比 /
prefers-reduced-motion)。**配色不采纳其 teal**——保留操作者指定的橘黄品牌(决策 D)。

## token 落点 (`src/theme.ts`)
所有颜色/字号/行高/间距/按钮尺寸集中到 theme; 组件不再散落硬编码。
- palette: primary 橘 #EF9F27 / deep #B45E14, success/error, warm-gray 中性
- typography scale: h6 18 / subtitle2 14 / body2 13 / caption 12 / button 13, 统一行高
- MuiButton: `minWidth:44`(触达目标下限) + padding 14 + `whiteSpace:nowrap`(根治 CJK 文案裁切)+
  focus-visible outline + 180ms transition
- MuiCssBaseline: prefers-reduced-motion 全局降级
- 工具栏窄屏 `flexWrap` + 主按钮 `flexShrink:0`; 搜索框 `flex:1 minWidth:200`

## 验收矩阵 (真实浏览器渲染, rsbuild dev + 9 条种子数据)
可复现审计(在页面 devtools 执行):
```js
[...document.querySelectorAll('button,.MuiChip-root,td,th,input')]
  .filter(el => el.scrollWidth > el.clientWidth+1 && (el.textContent.trim()||el.value))
```

| 页面 | 1200×800 | 960×640 |
|---|---|---|
| 表演者库 | 溢出 0 · 无横滚 | 溢出 0 · 无横滚 |
| 归档对比 | 溢出 0 · 无横滚 | 溢出 0 · 无横滚 |
| 设置 | 溢出 0 · 无横滚 | 溢出 0 · 无横滚 |

修复前: 「全部重扫」在 960 宽被 flex 挤压 4px(scrollWidth 72 > clientWidth 68)。
修复后: 6 组合全部零溢出。字号收敛到 {12,13,16,18}(caption/body/input/heading)。

## 无障碍对比 (Kimi 终审 P1/P2 修复, WCAG AA ≥4.5:1)
- 填充按钮: 橘黄 #EF9F27 底 + **深墨字 #33322F** = **5.77:1**(原白字仅 2.17:1); hover 保持浅橘 #E2921D 维持达标
- 次级/caption 文字: **#6B675E** on white = **5.67:1**(原 #8A867E 仅 3.4:1)
- 状态文字 ok/bad on white 达标
- 回归测试 `src/theme.test.ts` 锁死上述比值(4 断言)
- 触达目标: 按钮 minWidth 44; reduced-motion 只降 transition 保留 spinner
