# 最终构建的逐页测量（机器生成；每个标签取 measurements.jsonl 里最后一次记录）

说明：`横向滚动` 为 False 即 `documentElement.scrollWidth <= innerWidth`；`越界元素` 是矩形越过视口左右边界的元素数（若为横向滚动容器内部，记录里有 `clipped` 字段）；`<40x40 目标` 是按有效命中区域统计的数量；`首屏卡片 top` 与 `首屏可见截止时间` 用来判断首屏有没有有效信息。

| 标签 | 视口 | scrollWidth | 横向滚动 | 越界元素 | <40x40 目标 | 首屏卡片 top | 首屏可见截止时间 | 导航序号 |
|---|---|---|---|---|---|---|---|---|
| after-today-anon-360 | 360x780 | 360 | False | 0 | 0 | 505 | True | 1 |
| after-today-anon-390 | 390x844 | 390 | False | 0 | 0 | 508 | True | 1 |
| after-today-anon-430 | 430x932 | 430 | False | 0 | 0 | 508 | True | 1 |
| after-today-anon-se | 375x667 | 375 | False | 0 | 0 | 508 | True | 1 |
| after-today-360 | 360x780 | 360 | False | 0 | 0 | 459 | True | 1 |
| after-detail-360 | 360x780 | 360 | False | 0 | 0 |  | False | 1 |
| after-todo-360 | 360x780 | 360 | False | 0 | 0 |  | False | 1 |
| after-my-360 | 360x780 | 360 | False | 0 | 0 | 357 | True | 4 |
| after-add-360 | 360x780 | 360 | False | 0 | 0 |  | False | 4 |
| after-search-360 | 360x780 | 360 | False | 0 | 0 |  | False | 3 |
| after-today-390 | 390x844 | 390 | False | 0 | 0 | 461 | True | 1 |
| after-detail-390 | 390x844 | 390 | False | 0 | 0 |  | False | 1 |
| after-todo-390 | 390x844 | 390 | False | 0 | 0 |  | False | 1 |
| after-my-390 | 390x844 | 390 | False | 0 | 0 | 357 | True | 4 |
| after-add-390 | 390x844 | 390 | False | 0 | 0 |  | False | 4 |
| after-search-390 | 390x844 | 390 | False | 0 | 0 |  | False | 3 |
| after-search-zh-390 | 390x844 | 390 | False | 0 | 0 |  | False | 3 |
| after-today-430 | 430x932 | 430 | False | 0 | 0 | 461 | True | 1 |
| after-detail-430 | 430x932 | 430 | False | 0 | 0 |  | False | 1 |
| after-todo-430 | 430x932 | 430 | False | 0 | 0 |  | False | 1 |
| after-my-430 | 430x932 | 430 | False | 0 | 0 | 357 | True | 4 |
| after-add-430 | 430x932 | 430 | False | 0 | 0 |  | False | 4 |
| after-search-430 | 430x932 | 430 | False | 0 | 0 |  | False | 3 |
| after-today-1280 | 1280x900 | 1280 | False | 0 | 0 | 559 | True | 1 |
| after-detail-1280 | 1280x900 | 1280 | False | 0 | 0 |  | False | 1 |
| after-nav3-1280 | 1280x900 | 1280 | False | 0 | 0 |  | False | 3 |
| after-nav4-1280 | 1280x900 | 1280 | False | 0 | 0 | 407 | True | 4 |
| after-nav5-1280 | 1280x900 | 1280 | False | 0 | 0 |  | False | 5 |
| after-nav6-1280 | 1280x900 | 1280 | False | 0 | 0 |  | False | 6 |
| after-nav7-1280 | 1280x900 | 1280 | False | 0 | 0 |  | False | 7 |
| after-nav2-360 | 360x780 | 360 | False | 0 | 0 | 264 | True | 2 |
| after-nav5-360 | 360x780 | 360 | False | 0 | 0 |  | False | 5 |
| after-nav6-360 | 360x780 | 360 | False | 1 | 0 |  | False | 6 |
| after-nav7-360 | 360x780 | 360 | False | 1 | 0 |  | False | 7 |

首屏截止时间的绝对位置（top）：360x780 -> 640，390x844 -> 643，430x932 -> 643，375x667 -> 643（均小于视口高度，无需滚动）。
