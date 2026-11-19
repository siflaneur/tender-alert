# 交接文档：上海保险招标提醒（tender-alert）

更新时间：2026-11-19。写给接手这个项目的人或 agent，读完这份就能继续干活。
用户相关文档：[需求.md](需求.md)（需求）、[README.md](README.md)（使用说明）、[采购门户调研.md](采购门户调研.md)（企业门户调研）。

---

## 1. 项目是什么

用于在**上海的单位采购保险服务**时（车险、团体意外险、补充医疗、财产险、责任险、董责险等）第一时间收到提醒。

程序做的事：
1. 定时抓取多个招标网站的新公告
2. 按关键词和公告类型筛选
3. 跨网站去重
4. **每次运行都发一封邮件**（2026-11-02 用户要求）：有新保险公告时列出公告；没有时标题是「本次无新保险公告（新存档 N 条）」。两种邮件末尾都附一行「本次运行情况」汇总（几个来源、几个正常/失败/今天已抓过、新存档多少条），不逐个列来源，只把失败的来源和原因单独列出（2026-11-18 用户要求精简）。没配邮箱时只存成网页预览放在 `previews/`
5. **全量存档**（2026-11-02 起）：抓到的**所有上海公告**（不限保险、所有类型含中标结果）都存进数据库 `archive` 表，供本地历史检索与统计分析；开关在 config.toml 的 `[archive]`
6. **往年续标与曾经中标公司标记**（2026-11-18 起）：对准备发信的新命中公告自动回查近 3 年（兼容 1 年期与 2–3 年期续保标），一次性工程险（建安一切险、工程质量潜在缺陷险等）标为 `one_off` 跳过查询；找到上一轮结果/合同公告时提取曾经的中标公司（或第一候选人）并在邮件标题下方和历史 CSV 中标记

**当前阶段：本地运行（阶段一）**。上云（阶段二）以后再说，需求见 需求.md §7。

## 2. 当前状态

| 项目 | 状态 |
|---|---|
| 代码 | 18 个数据来源都已实现、启用 17 个（金采网 cfcpn 已停用；edu 含 29 所高校，orgs 含 21 家企事业/金融基础设施/招标代理，子单位支持独立连续失败告警），含往年续标与曾经中标公司核查模块 `prior.py`，177 个单元测试全部通过（含 16 个启用 HTTP 来源及 cfcpn 的真实响应回放，申通走无头浏览器无样本） |
| 真实运行 | 截至 2026-11-18 22:04 持续稳定发信，17 个在用来源全部正常（0 连续失败）；orgs（21 家）已完成首次正式抓取并发出 3 条新命中 |
| 邮箱 | **已配置**：SMTP 发信（如 QQ 邮箱 `smtp.qq.com:465`），发信/收件邮箱与授权码均在本地 `.env` 配置，不进 git（模板见 `.env.example`） |
| 定时任务 | **已注册**：每天 **10:00、15:00、22:00** 各运行一次（2026-11-04 用户确认，之前是 10:00 和 20:00 两次）。2026-11-05 起任务开了「唤醒电脑运行」，用户已启用 Windows 的「允许使用唤醒定时器」（只开插电，电池时仍禁用），实际唤醒还没验证（见 §9）。重新注册脚本是 `install-task.ps1`（2026-11-19 已重新注册，45 分钟超时上限已生效） |
| git | 2026-10-30 装好（`C:\Program Files\Git\cmd\git.exe`），仓库在项目目录，主分支 main，已关联 GitHub 公开仓库 `origin`（`https://github.com/siflaneur/tender-alert`）。`data/` 和 `.env` 不进 git，数据库靠同级 `../tender-alert-backups/` 里的快照保存。提交规则见 CLAUDE.md |
| 数据库 | `data/tender-alert.db` 正常（截至 2026-11-18 22:04）：`notices` 入库 77 条（**61 条已提醒、0 条待提醒**、16 条跨站/同站重复合并），`archive` 全量存档 **18,896 条**。快照保存在 `tender-alert-backups/`（最新为 `tender-alert-data-20261118-221000-before-opt.db`） |

## 3. 运行环境

- Windows 11，PowerShell 7。会话是非交互的，不能用 `Read-Host` 这类命令。
- Python 3.12 装在系统里，但**一律使用项目里的 `.venv`**：`.\.venv\Scripts\python.exe`
- 依赖见 requirements.txt：requests、beautifulsoup4、lxml、python-dotenv
- 申通地铁来源还需要本机装有 Chrome 或 Edge（用无头模式渲染它的公开首页），不额外安装 Python 包
- 项目路径：本地项目根目录（`tender-alert/`）

常用命令（在项目目录下运行）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests   # 跑测试
.\run.ps1 --dry-run -v                                     # 试运行：不发信、不改数据库，-v 显示被筛掉的标题
.\run.ps1 --dry-run --sources ceair,bosc                   # 只跑指定来源
.\run.ps1 --dry-run --record                              # 试运行并把真实响应录成测试样本（tests/fixtures/）
.\run.ps1                                                  # 正式运行（会改数据库；配了邮箱就会发信）
.\run.ps1 --test-mail                                      # 发测试邮件
```

## 4. 代码结构

```
tender_alert/
  __main__.py      主流程：逐个来源抓取 → 归类 → 回看窗口过滤 → 关键词匹配 → 入库去重 → 汇总发信
  config.py        读 config.toml 和 .env
  models.py        Notice 数据结构；classify_kind 把各站的公告类型归成 tender/correction/result/intent/other
  filters.py       in_window（回看窗口）、match（关键词/排除词/类型/地区）、still_matches（发信前按当前配置再筛一遍）
  prior.py         往年同标续标核查（回看近 3 年，自动跳过一次性工程险）及曾经中标公司提取，结果写入 notices.extra["prior"]
  store.py         SQLite：notices（保险命中、去重和待提醒）、archive（全量存档，不限保险）、source_health（连续失败与告警）、source_state（有状态来源的进度）
  export.py        导出：保险命中历史 → exports/历史命中公告.csv（每次正式运行后）和 JSON（--export-json）；
                   全量存档 → exports/全量存档/YYYY-MM.csv，按发布月份拆分，每次只重写有变动的月份（--export-archive 全部重导）
  mailer.py        生成邮件 HTML、SMTP 发送（QUIT 阶段的错误会忽略，见 §7）
  http.py          PoliteSession：按域名限速，5xx 和网络错误自动重试；429/503 时按 Retry-After 等待并把该域名间隔加倍（最多 60 秒）；
                   每个请求先过 robots 检查；挂了 recorder 时把返回的响应录下来
  robots.py        RobotsGuard：每个请求发出前按 robots.txt（RFC 9309 规则）检查，禁止就抛 RobotsDisallowed、不发请求；
                   结果缓存在 data/robots_cache.json 7 天；用户授权的例外在 EXCEPTIONS（只能由用户本人确认后改）
  recording.py     --record 用的录制器：把来源收到的响应按顺序存成 tests/fixtures/<来源>.json.gz
  sources/
    __init__.py    来源模块的约定（见下）
    _common.py     outside_shanghai（按标题判断是否外地分支）、LegacyTLSAdapter（兼容旧式 TLS）
    zfcg_sh.py     上海政府采购网
    airport.py     上海机场集团·阳光采购
    shggzy.py      上海公共资源交易平台·国企采购
    shggzy_zhcg.py 上海公共资源交易平台·综合采购（有状态）
    shmetro.py     上海申通地铁·采购电子商务平台（用无头浏览器渲染公开首页）
    ceair.py       东方航空·集采实施平台
    bosc.py        上海银行·官网招标采购（有状态）
    gtht.py        国泰海通·供应商门户
    srcb.py        上海农商银行·供应商门户（有状态）
    ebscn.py       光大证券·集中采购管理平台（有状态）
    baowu.py       中国宝武·宝华智慧招标共享平台（只保留上海项目）
    cosco.py       中远海运·集团采购信息系统（只保留上海项目）
    chengtou.py    上海城投集团·招采服务平台
    sse.py         上海证券交易所·采购门户
    cfcpn.py       金采网·金融集中采购网（只保留上海；**2026-11-11 用户要求停用**，不在 config.toml 的 enabled 里）
    edu.py         上海高校·采购招标公告（29 所：通用列表解析器 parse_list + SCHOOLS 名单；交大、工程技术大学走 fetch_platform）
    orgs.py        上海企事业单位·自有采购网站（21 家：复用 parse_list + 上海信托/国际招标/中世建咨公开列表接口）
    ccgp.py        中国政府采购网（有状态：分别记录保险搜索与在沪中央单位全量搜索水位）
tests/             test_core.py、test_shggzy_zhcg.py、test_enterprise_sources.py、test_new_enterprise_sources.py、
                   test_airport_source.py、test_chengtou_source.py、test_shmetro_source.py、test_finance_sources.py、
                   test_edu_source.py、test_orgs_source.py、test_prior.py、test_network.py、test_export.py、
                   test_full_lists.py、test_robots.py、test_recorded_fixtures.py（用 replay.py 回放 fixtures/ 里的真实响应）
config.toml        关键词、排除词、提醒类型、回看天数（30）、启用的来源及优先级
.env               邮箱配置（本地填写，不进 git；模板是 .env.example）
run.ps1            手动运行入口
install-task.ps1   注册 Windows 定时任务：每天 10:00 / 15:00 / 22:00 用 pythonw 运行，没有窗口
data/ logs/ previews/ exports/   运行时生成，都在 .gitignore 里；exports/历史命中公告.csv 是给用户看的历史结果
```

### 来源模块约定（新增来源照着写）

每个来源是一个模块，提供：
- `NAME`：显示名称
- `REGION_SCOPED`：True 表示抓回来的都是上海的，或者模块自己已经按地区过滤过
- `fetch(session, cfg, since) -> list[Notice]`

需要记住进度的来源再加 `STATEFUL = True`，`fetch` 多收两个参数 `state: dict` 和 `dry_run: bool`：原地修改 `state`，抓取成功后主流程会存回数据库；dry-run 时不存。

其他要求：
- `kind` 能确定就直接设；设成 `"other"` 的，主流程会调用 `classify_kind` 按标题归类。
- 页面结构不对、接口报错时**一定要抛异常**，不要安静地返回空列表。连续失败 3 次会发告警邮件。
- 在 `config.toml` 的 `enabled` 里加上新来源的 key。排在前面的来源优先：同一条公告出现在多个网站时，只按优先的那个提醒一次。

### 去重和提醒的规则
- 同一来源内：用 `(source, source_id)` 判断。
- 标题去掉空白和标点后相同、发布日期相差不超过 3 天，就算同一条。**同一来源内部也适用**：东航平台上同一个项目常有几条 id 不同、标题日期完全一样的记录。
- **不同网站之间再放宽一步**（2026-11-04）：转载时常改写标题末尾的公告类型（申通「…项目招标公告」在综合采购是「…项目采购公告」，「中标结果公告」变「成交结果公告」），所以 `normalize_title` 会去掉末尾的类型字样只比项目名称，同时要求两条的 kind 相同（招标对招标、结果对结果）。同一网站内、或有一边 kind 是 other 时不放宽，仍要完整标题相同：宝武候选人公示「X」和「X中标结果公告」、政府采购网合同公告和履约验收、东航「X」和「X（终止）」都是不同的公告。终止、废标、流标、失败、候选人、重新、第二次不算后缀。用全量存档 3691 条压力测试过：新合并的 16 对全是申通/上海银行与综合采购的真重复。数据库里已有记录的 norm_title 每次启动时按新规则重算。
- 只有真的发出去后才标记为「已提醒」。发送失败会写日志和预览，下次运行重发。

## 5. 各数据来源要点

| key | 网站 | 抓取方式 | 坑和限制 |
|---|---|---|---|
| zfcg_sh | 上海政府采购网 | `POST /portal/all`，标题搜「险」，按公告类型分别查 | 每页最多 15 条；不查采购合同公告（每天几十条车险框架协议合同，太吵） |
| airport | 上海机场集团·阳光采购 shanghaiairport.com/zhaobiao | `POST /AvinexApi/Pc/SunshineProcurementHandler.aspx`，按标题关键字（险/医疗/保障）搜，`time` 年份可空 | 先 GET 列表页拿加速乐 Cookie 再调接口；详情页对新公告 404/暂无数据，邮件链接指向列表页；网站声明「未经许可禁止转载」，只做个人提醒、不转载正文 |
| shggzy | 国企采购子站 gqcg.shggzy.com | 全文检索 JSON 接口，标题关键词之间是「或」 | 平台 2026-04 才上线，数据量小 |
| shggzy_zhcg | 综合采购 www.shggzy.com/jyxxwzcg | **只翻列表页**，记住看到的位置，下次翻到那里就停 | 见下面「综合采购的特殊规则」 |
| shmetro | 申通地铁 eps.shmetro.com | 用本机 Chrome/Edge 无头渲染公开首页，从 DOM 读各栏目最新 10 条公告 | **robots.txt 整站禁止，用户 2026-10-31 授权忽略**；接口要 RSA `sign` 签名，**不复刻、不登录**；无详情链接，邮件链到首页；每栏目只有 10 条，靠每天增量 |
| ceair | 东航 caigou.ceair.com | `POST /portal/portal/sysInfoList`，只搜「险」和「医疗保障」两次 | **有频率限制**：连搜 7 次就会返回「操作频繁，请6分钟后重试」。外地分公司按标题过滤。页脚写着「不得转载」：只做个人提醒，不转载内容 |
| bosc | 上海银行 bosc.cn | 静态列表页 `index.shtml`、`index_1.shtml`…，每页 20 条，记住看到的位置 | 外地分行按标题过滤 |
| gtht | 国泰海通 supplier.gtht.com | `GET /api/index/notice?title=险` | 服务器用旧式 TLS 重协商，必须用 LegacyTLSAdapter。保险采购集中在每年 3 月前后 |
| srcb | 上海农商银行 pms2g.shrcb.com | 静态列表页 `index.html`、`index_2.html`…，记住看到的位置 | 每页 10 条；保险采购包括董监高责任险、员工补充医疗等 |
| ebscn | 光大证券 purms.ebscn.com | 公开搜索表单搜「险」，命中关键词后再查详情落款日期 | 列表只显示截止时间；详情落款晚于截止时间时退回截止日期 |
| baowu | 中国宝武·宝华招标 baohuabidding.cn | 公开全文检索接口按标题和 `sdt/edt` 日期搜索「险」「补充医疗」「医疗保障」「承保」 | 全国项目混排，只保留标题、招标人或项目地点明确属于上海的；外地基地在标题命中时直接排除，不明确时再查详情 |
| cosco | 中远海运 procurement.coscoshipping.com | 公开公告接口按创建时间分页读取完整正文，用标题关键词筛选 | 全国公告混排，只保留接口 `region/province` 为上海的；正文里出现上海供应商或收货地址不算上海项目 |
| chengtou | 上海城投集团·招采服务平台 zhaocai.chengtoucct.com | `POST /fcsrm/fcb/bid/open/notice/supplier/queryPage`，size=100 全量翻页，翻到回看窗口之前就停 | 无需登录、无反爬；没有独立详情页（点是弹窗），邮件链接指向公告列表页；量少（近 3 个月 40 多条） |
| sse | 上海证券交易所·采购门户 www.sse.com.cn/purchase | 「公开征集」栏目静态页 `/purchase/information/`，只取第一页（约 100 条、四五个月） | 标题是「供应商征集公告（项目名）」；「结果公示」栏目由 query.sse.com.cn 接口加载，**还没接** |
| cfcpn（已停用） | 金采网 www.cfcpn.com（全国金融机构采购）。**用户 2026-11-11 说「别爬了，都是金融机构发布的」，已从 enabled 里去掉，没有用户要求不要恢复** | `POST /jcw/noticeinfo/noticeInfo/dataNoticeList`（`beginPublishTime` 带了但接口不认，靠翻到回看起点就停）；查两遍：`areaProvince=310000`（上海），和 `noticeTitle=上海` 里地区不是上海的 | 网站只有 http。**有频率限制**：连续请求六七次后返回 403（浏览器访问不受影响），所以每种查法最多 3 页、间隔 5 秒，一次运行最多 6 个请求，不补历史；中途 403 保留已拿到的，第一页就 403 才算失败。「标题含上海」会带出「上海银行盐城分行」「浦发XX分行」，用 `_elsewhere` 去掉。「采购方式」字段和公告常对不上，kind 先按标题判断。和 bosc/gtht/ebscn/srcb 重复的靠跨站去重。详情页免登录只显示预览。页面写着「未经允许，不得转载」。**保险公司自己的采购会误报**：如「太平财险上海分公司车险部…车险代为驾驶服务」，机构名去掉后仍带「车险」，先观察 |
| edu | 上海 29 所高校各自的采购/招标网站（名单在 `edu.SCHOOLS` 和 `edu.PLATFORM_SCHOOLS`） | 每校 1 到 2 个静态列表页，只取第一页；通用解析器 `parse_list` 找「标题链接 + 同一行里的日期」，标题要带采购招标字样 | 一个来源、42 个请求、约 1 分半钟。上海交大和上海工程技术大学用同一套「数字化采购平台」（思必得 sfw_cms）：先 GET 列表页（拿会话和页面里的 `ops_uuid`），再 `POST /sfw_cms/e`（`page=cms.psms.publish.query`，参数照抄网页各栏目自己发的），每次查询取最新 50 条；交大不带类型条件会返回空，工程技术大学不带条件就是全部。上海海事大学的行里只写「11月03日」，年份从文章地址 `/2026/1103/` 里取。`source_id` 是「学校 key:路径」，**key 定了不要改**。单个学校失败只记警告，三分之一以上失败才整体报错；单个学校连续失败达到阈值也会独立发告警邮件。加学校：往 SCHOOLS 加一行，再重录样本。没接的高校和原因（2026-11-11）：上海理工大学（招标公告栏目循环重定向，结果栏目停在 2023 年）、上海立信会计金融学院（返回 403「已被网站管理员设置拦截」，不绕）、上海开放大学（返回 412，带访问挑战，不绕）、上海视觉艺术学院（停在 2024 年）、上海交大医学院（只有季度汇总表）、上海体育大学（没找到公告栏目）；高职院校和民办高校这次没查。 |
| orgs | 上海企事业单位、金融基础设施及头部招标代理自有网站（共 21 家，名单在 `orgs.ORGS` 与 `orgs.API_ORGS`） | 18 家走静态列表页（复用 `edu.parse_list`），3 家（上海国际信托、上海国际招标 SITC、上海中世建设咨询）走网页公开免登录 JSON 接口；每天抓一次 | 一次共 30 个请求（跨 21 个域名，约半分钟）。覆盖 SMG、复旦肿瘤/华山医院、中科院上海三所、中国银联、上海清算所、上期所、上金所、城银清算、CIPS、中银国际证券、上海国际信托、上海地产集团，以及国际招标（SITC）/中世建咨/机电招标/财瑞/百通 5 家代理、中烟平台（只留标题带「上海」的）。对银联及各代理机构启用 `outside_shanghai` 剔除外地分公司/外省项目；`source_id` 是 `key:路径或ID`，**key 定了不要改**；单个单位失败只记警告，三分之一以上失败才整体报错；单个单位连续失败达到阈值也会独立发告警邮件。 |
| ccgp | 中国政府采购网 | 先打开首页，再调用 search.ccgp.gov.cn 的标题搜索，用 **http** 不用 https | 有频率限制（会返回「访问过于频繁」）；https 对脚本有 CDN 拦截，站内链接本来就都是 http。`STATEFUL=True` 分别记录保险搜索（保持 7 天回看窗口）与在沪中央单位全量搜索水位（同日只查当天，遇限流不推进水位） |

### 全量存档模式（config.toml `[archive] enabled = true`，2026-11-02 起）

每条抓到的上海公告都进 `archive` 表；保险命中照旧进 `notices` 表并提醒。来源声明 `INCREMENTAL = True` 时，主流程只让它从上次成功的前一天抓起（`Store.last_success_date`）。各来源在存档模式下的抓法：

| key | 存档模式抓法 | 量（实测，约两天） |
|---|---|---|
| zfcg_sh | `POST /portal/category` 按 12 个公告类型列全部（不抓「网上超市」小额订单），一页 100 条，翻到 since 前停 | 943 条，18 个请求 |
| shggzy | 去掉标题关键词条件 | 2 条 |
| gtht | 不带 title，采购公告和结果公告都抓 | 56 条 |
| airport | key 留空列全部，翻到 since 前停 | 100 条 |
| ceair | title 留空列全国全部（一天约 300 条），一页 100 条、间隔 8 秒、最多 8 页，外地按标题过滤 | 103 条（过滤外地 97 条） |
| ebscn | 不带关键词取最新 50 条；发布日期要开详情页，取过的缓存在 state；非保险公告每次最多新开 20 个详情页 | 每次 20 条左右 |
| baowu | 不带标题条件抓全国，非保险项目只凭标题和 800 字摘要判断上海，不额外请求 | 全国 157 条中 21 条 |
| ccgp | 保险搜索照旧；另加一次「中央单位 + 上海、不带关键词」搜索（地方公告是 zfcg_sh 的转载，不重复抓），间隔 5 秒 | 53 条 |
| 其余 | 综合采购（开存档后含成交结果栏目）、上海银行、农商行、中远海运、城投、申通本来就抓全量 | — |

### 综合采购的特殊规则（用户逐条确认过，别改）
- 网站 robots.txt 写的是 `Disallow: /search/` 和 `Disallow: /jyxx*/`。
- 日常**只读列表页**，比如 `/jyxxwzcggg`、`/jyxxwzcggg_2.jhtml`。这些路径字面上不在禁止范围内。
- **不打开详情页**。邮件里的链接是给人点的，链接带着列表页给的 cExt 令牌，大约 3 周内有效。
- 每页间隔 4 秒，**每个栏目每次最多翻 10 页**。第 11 页起网站会弹滑块验证码（2026-10-30 实测）。遇到验证码就报错停下，**绝不尝试通过**。
- **`/search/` 只用了一次**：用户同意在第一次正式运行时，用网站搜索补齐最近 30 天的历史。2026-10-30 已经补过，记录在 `source_state` 的 `backfilled` 里。**不要再增加任何使用 `/search/` 的地方。**
- 已知局限：采购公告每天 100 多条，每次最多看最新 100 条。现在每天跑三次（10:00 / 15:00 / 22:00，2026-11-04 用户确认），每次间隔约 5–9 小时、新增一般 30–50 条，基本不会顶到上限；只有长时间关机后补跑才可能漏中间。仍建议用户同时在官方 App 里订阅「保险」作为兜底。

## 6. 硬性规则（用户和安全层面的约束）

1. **不绕过任何反爬措施**：不通过验证码，不模拟 JS 挑战，不轮换 IP，不伪造挑战 cookie，**不解密网站有意加密的接口数据**。浦发银行就是因为这一条才没有接入。
2. **robots.txt 禁止的路径，必须先问用户**。现在由 `tender_alert/robots.py` 在代码里强制检查，授权例外只能写进它的 `EXCEPTIONS`，**新增或放宽例外必须由用户本人确认**。综合采购的做法是用户在知情的情况下选择的。中国招标投标公共服务平台（ctbpsp.com）整站禁止抓取，用户没有要求接入。
   **申通地铁 eps.shmetro.com 是用户 2026-10-31 明确授权的 robots 例外**：整站 `Disallow: /` 仍接入，但只用无头浏览器渲染公开首页取公告标题，**不复刻它的接口 sign 签名、不登录**。
3. **注意礼貌**：每个网站至少间隔 2 秒，有限流的站点放到 3 到 5 秒。调试和调研时同样适用，**包括下载网站的 JS 包来找接口**：一次只下需要的文件，文件之间也要间隔，不要循环批量下载整站资源（2026-10-30 Claude 调研国泰海通时曾无间隔下载约 110 个 JS 文件，属于违规；事后查明门户故障与此无关，但规则照旧）。
4. 发真实邮件、注册定时任务、安装软件、删除用户数据、push 到远程仓库：都要先问用户。
5. 删除之前先列出要删的范围。
6. 沟通与开发偏好：用中文沟通；不熟悉的功能先去 GitHub 找同类开源项目对照。
7. **全量存档数据仅限本地自用分析**：上海公共资源交易平台、东航等站点声明「未经许可禁止转载」，综合采购 /search/ 补抓和申通地铁两个 robots 例外也是按个人使用授权的。agent 不要自行把存档数据发布、同步到外部服务或做成对外接口。

## 7. 踩过的坑

- **QQ 邮箱对 SMTP QUIT 的回复不规范**，smtplib 会在信已经发出之后再抛异常。mailer.send 已经处理：sendmail 返回就算成功。不要改回 `with server:` 的写法。
- **WebSearch 和 Exa 对中国企业门户效果很差**，Exa 免费额度很快就用完。找国内企业的采购门户，用内置浏览器打开 `https://cn.bing.com/search?q=...` 最有效。
- 很多国内站点是单页应用，接口要用内置浏览器的 `read_network_requests` 看，或者翻它的 JS 包。
- 上海很多路名是外地地名（南京路、江苏路、四川北路）。`outside_shanghai` 的处理是：标题带「上海」就保留，但「XX 分行 / 分公司 / 基地」前面是外地名的一律剔除。
- 用单字「险」搜索会带出「风险评估」之类的结果，靠 `filters.match` 的关键词过滤掉，这是正常的。
- 「员工综合医疗保障」这类团体保险项目的标题里没有「险」字，所以关键词里加了「医疗保障」，排除词里加了「医疗保障局」「医疗保障事务」。但上海政府采购网、国泰海通在服务端只搜「险」，这类标题在这两个来源可能漏掉。
- 在 PowerShell 里写多行提交说明：`@'...'@ | git commit -F -` 这种管道写法传不进去。先把说明写进一个文本文件，再用 `git commit -F <文件路径>`。
- **电脑开机或唤醒后，定时任务会立刻补跑，这时网络往往还没就绪**，所有网站都返回「远程主机强迫关闭了连接」（2026-10-31 15:56 那次就是这样）。现在主流程开始前会调用 `http.wait_for_network` 等网络，最多等 3 分钟；等不到就整次跳过，不计入任何来源的失败次数，免得误发告警。
- 申通地铁只在每天 7:00–19:00 开放，其他时间 `shmetro.fetch` 直接跳过、不算失败。
- **回放测试失败**（`tests/test_recorded_fixtures.py`）多半是网站改版或代码改了请求顺序：先确认代码改对了，再 `.\run.ps1 --dry-run --record` 重新录样本一起提交。回放只核对方法、域名和路径，查询参数里的日期每天不同、不核对。申通地铁走无头浏览器，没有样本。
- 国泰海通 supplier.gtht.com 的 robots.txt 返回 500：按规则本次放行、不缓存，日志里每次一条警告，属正常。
- 测试里的 FakeSession 没有 `interval`、`headers` 属性；来源代码会读 `session.interval`、`session.headers`，所以测试的 FakeSession 里要手动补上（`interval = 0`、`headers = {}`）。

## 8. 待办（按优先级）

1. **用户观察**：看提醒邮件有没有误报或漏报，需要时调整 config.toml。综合采购每次最多看最新 100 条，每天三次基本够，偶尔（长时间关机后补跑）可能漏，建议同时在「上海公共资源交易」官方 App 订阅「保险」兜底。
1.5 **全量存档第二步已完成（2026-11-02）**：所有来源在存档模式下都抓全量，抓法见下面「全量存档模式」。全量 dry-run 一轮存档 1793 条、76 个请求、约 3.5 分钟。存档数据的按月 CSV 导出也已完成（exports/全量存档/）。
2. 国泰海通：2026-10-30 23:21、23:24 两次 TLS 握手被服务器直接重置（Python、curl、真实 IP 都试过）；上次成功是当天 21:50。由 Claude 跟进（用户 2026-10-31 指定）。
   **2026-10-31 Claude 排查**：主站 www.gtht.com 正常（200），只有 supplier.gtht.com 握手失败；内置浏览器打不开，**用户用手机 5G（不同网络、不同 IP）也打不开** → 是**门户网站本身故障或维护**，不是本机 IP 被封。
   **2026-10-31 19:05 已恢复**：正式运行时正常抓到 30 条。
   处理：程序这边不用改，每天自动重试一次，恢复后自动接上；连续失败 3 次会发一封告警邮件，恢复前不再重复发。如果一两周后还不恢复，再查是否换了域名或门户地址。

3. 第三批：**上海机场集团（airport）、上海城投集团（chengtou）已于 2026-10-31 接入**。剩余：
   - **已接入**：申通地铁 eps.shmetro.com（2026-10-31，shmetro）—— 用户授权无视它的整站 robots 禁止，实现方式是用本机无头浏览器渲染公开首页；接口有 RSA 签名，没有去破解；每栏目只有最新 10 条，靠每天增量
   - **不接**：上港集团（公开数据 505/555 条标题打码、bidId 为空）、申能集团（门户只开登录页、接口要 OAuth token）
   - **先观察**：上海电气（全库 62 条、保险 0，招标栏目未上线）、城投水务（公开列表可看，但接口要 access_token 签名）
4. 阶段二上云：GitHub Actions 的服务器在境外，可能被这些网站屏蔽，需要先验证；屏蔽的话改用国内云函数或轻量服务器。综合采购和上海银行的进度存在 SQLite 里，上云时要考虑怎么持久化。
5. 第五批及后续代理/金融基础设施来源（详见 `需求.md` §12 与 `采购门户调研.md`）：**2026-11-18 已全部落地至 `orgs`（共 21 家）**——上午接入首批 12 家（SMG、复旦肿瘤/华山医院、中科院三所、城银清算、CIPS、中银国际证券、上海国际信托、机电设备招标、中烟上海），中午继续补齐 4 家头部代理（上海国际招标 SITC、上海中世建设咨询、上海财瑞、上海百通）、4 家金融基础设施（中国银联、上海清算所、上期所、上金所）及上海地产集团。剩余结论：
   - **不接（用户 2026-11-18 决定）**：农信银资金清算中心（`nongxinyin.com`）——调研时记录它 2026-11-09 有「员工补充医疗保险」中标公示，是第五批里线索最硬的；但 2026-11-18 复测**逐页扫了通知公告 16 页 63 条（2024-10-09～2026-11-16），没有一条标题带保险/医疗**，其他栏目和站内搜索也查不到，线索核实不上。加上是低频站（2026 年只发 6 条）、robots.txt 两个 `User-agent: *` 组一条 Allow 一条 Disallow 有争议，用户决定不接。
   - **不接（暂时）**：上海科技馆 `group.sstm.org.cn`（有「三馆综合保险」公开招标）——2026-11-18 复测仍 504。已查明根因：科技馆官网 `www.sstm.org.cn` 的「招标公告」页是 iframe 套 `group.sstm.org.cn/publicity/purchase`，而集团站整站 504（`sstm.org.cn`、`bidding.sstm.org.cn`、http/https 都一样）；官网自己的 API `/prod-api/visit/moreXzw/biddingNotice` 返回 401 要登录，不绕。**等集团站恢复再看。**
   - **不接（2026-11-18 已逐个验证）**：央企在沪和市属集团这批**自建平台/官网普遍接不了**——
     - 中建八局·云筑网 `yzw.cn`：umi SPA，JS 里只有登录/验证码接口，无公开招标列表
     - 隧道股份 STECmart `stecmart.stec.net`：200 但只有 1646 字节空壳，纯前端渲染，robots.txt 也返回那个空壳
     - 上海建工·营造商 `yzscg.cn`：`:8443` 仍 504；`http://` 是 Vue SPA，同域 API 调用 TLS 失败
     - 百联集团：有「公告栏」但搜不到招标/采购字样；光明食品：确有采购公告但混在新闻流里没有独立栏目；上海医药：只有港交所公告与通函；锦江国际：首页 616 字节 SPA 空壳；东浩兰生：TLS 握手失败
   - **后续跟进**：观察 3–5 天 `orgs`（21 家）正式入库与提醒情况（15:00 定时任务起首次正式跑）；上海东松医疗（`shdongsong.cn/dstender/` 纯登录页）、上海社发（`sf.sh-yuanzhu.com` 空壳 SPA）已验证不可接。其余低频可选项仅剩上交所「结果公示」与人民银行采购网。
   - **不建议再接**：文化场馆官网、公交出租、国网上海/上海电力/上海燃气官网、基金公司官网、市属金控官网、华鑫证券（TLS 失败）/申万宏源（521）/爱建证券（robots 禁 main）/九院（JS 挑战）。理由见 `需求.md` §12.4。

## 9. 最近一次操作

### 2026-11-18 12:40 核查命中与存档质量、扩展 orgs 至 21 家并同步交接文档

- 全量复盘 58 条历史已提醒公告与 17,624 条存档：补上「综合险、医责险、安责险、董责险」等简称关键词（接住此前漏报的杨浦区「残疾人阳光综合险」公开招标），排除词补「虚拟仿真」与「医疗保障信息平台」（消除高校教学软件与医保中心运维两条误报）；`classify_kind` 把「失败」归入 `result`，`shmetro`「招标公告」栏内的变更/延期公告归入 `correction`
- 核查上午接入的 `orgs` 首批 12 家全部解析正常（命中 SMG 车辆保险比选）；随后探明并补入上海国际招标（SITC）、上海中世建设咨询、上海财瑞、上海百通 4 家头部代理，以及中国银联、上海清算所、上期所、上金所、上海地产集团，合计 21 家。真实 dry-run 抓到 238 条、新命中 3 条（新增银联员工团体保险采购公告及 11-17 更正公告）；160 个测试全部通过
- 运行前已为数据库打快照 `tender-alert-backups/tender-alert-data-20261118-120300.db`；当天 15:00 定时任务将首次正式运行 `orgs`

### 2026-11-11 17:00 停用金采网

- 用户要求不再抓金采网（都是金融机构发布的）。从 config.toml 的 enabled 里去掉，代码、测试和回放样本保留
- 它从来没有正式运行成功过，数据库里没有它的存档和命中记录，不用清理
- 每次都抓的来源剩 5 个：zfcg_sh、shggzy_zhcg、shmetro、ceair、ccgp

### 2026-11-11 16:50 按更新快慢分抓取频率；金采网限流

- 用户要求：更新快的网站每天抓三次，慢的每天一次。config.toml 新增 `[sources] once_a_day`，名单里的来源当天抓成功过就跳过（`__main__.already_done_today`）；当天没成功的照常抓，所以早上失败下午会补。`--sources` 点名和 `--record` 不跳过
- 分法依据存档里 11 月上旬每个工作日的发布量。每次都抓 6 个：zfcg_sh、shggzy_zhcg、shmetro、ceair、cfcpn、ccgp；每天一次 11 个：airport、shggzy、bosc、gtht、srcb、ebscn、baowu、cosco、chengtou、sse、edu。申通地铁量不大但每栏只显示最新 10 条，必须多抓
- 效果：第二、三次运行少抓 11 个来源，高校那 42 个请求一天只发一次
- 顺带发现金采网 15:00 第一次正式运行失败（补 30 天翻到第 7 页被 403），已改成每次最多 6 个请求，见 §5 cfcpn 一行。**金采网到现在还没有正式运行成功过**，22:00 那次是第一次

### 2026-11-11 16:30 高校补接三所：上海交大、上海工程技术大学、上海海事大学

- 用户点名要这三所。交大和工程技术大学是同一套采购平台，列表走公开 JSON 接口；海事大学其实是静态页，只是日期不带年份
- 真实 dry-run（回看 30 天）：全部 29 所 42 个请求抓到 805 条，其中交大 100、工程技术 50、海事 63
- 注意：11-11 15:00 的定时运行已经用了还没提交的这版代码（工作区里的改动会直接被定时任务用到），那次高校抓到 802 条并存档，运行正常

### 2026-11-11 12:00 接入上海高校（edu，26 所）

- 用户要求把上海高校都接上（给全量存档用）。查了 35 所本科和专科院校的采购网站，26 所能用同一个通用解析器接，9 所没接（原因见 §5 edu 一行）
- 真实 dry-run（回看 30 天）：32 个请求抓到 585 条（上海大学 98、复旦 69、同济 63…），招标类 274、结果类 235；保险类只有 1 条结果公示（上海科技大学外籍职工补充医疗保险），不在提醒类型里
- 各校网站都没有 robots 限制；立信、开放大学有拦截，没有绕

### 2026-11-11 11:40 接入上交所采购门户和金采网

- 用户从第四批调研里选了这两个先接。两个都没有 robots 限制，都是公开页面/接口
- 真实 dry-run（回看 3 天）：上交所 5 条、命中 1 条（数据公司公务用车车险）；金采网 6 个请求抓到 165 条（上海地区 157 条，标题含上海的外地注册机构 8 条）、命中 1 条（是保险公司自己买代驾服务，误报）
- **第一次正式运行会补 30 天**：金采网约 50 个请求、3 分钟左右，邮件里会一次性出现上交所和金采网过去 30 天的保险类公告（上交所那 3 条里有的可能已过截止时间）
- 还没做：上交所「结果公示」、第四批里其余来源（清算所、上期所、黄金交易所、银联、人民银行采购网）

### 2026-11-11 第四批来源调研（只调研，没接入）

- 用户想扩展信息来源。结论写在 `采购门户调研.md`「第四批调研」：在沪金融基础设施（上交所、上期所、黄金交易所、清算所、银联等）是一整块空白，上交所采购门户首页就有 3 条保险类公告我们没抓到；金采网（全国金融机构采购，公开、可按地区筛）覆盖面最大
- 接哪些等用户决定；每个来源接入前仍要看转载声明、确认接口

### 2026-11-05 20:45 定时任务打开唤醒电脑运行

- 11-05 电脑 08:28 到 20:22 一直睡眠，10:00 和 15:00 两次错过，醒来约 6 分钟后补跑一次（20:28，发出 1 条新公告）；申通地铁因超出开放时间跳过，当天没抓到
- 用户要求改：`install-task.ps1` 加 `-WakeToRun`，已重新注册并核对，下次 11-05 22:00
- 这台笔记本的「允许使用唤醒定时器」原来插电和电池都是禁用。这是系统设置，agent 不改；用户 11-05 20:45 自己启用了插电时的，已用 powercfg 核对（交流 1、直流 0）
- **待验证**：这台机器是现代待机（S0），能否按时醒来要看第一次插电睡眠跨过运行时间后的日志；用电池时不会唤醒，靠醒来后补跑

### 2026-11-04 21:00 修复跨站去重漏洞

- 用户发现申通地铁自己的招标会同步到上海公共资源交易平台。对比存档：申通的正式招标和中标结果约一半在综合采购上也有（标题后缀被改写），竞价和单一来源公示完全不同步，所以申通来源要保留
- 发现漏洞：「2026-2028年上海地铁维护保障有限公司职工商业保险项目」申通叫「招标公告」、综合采购叫「采购公告」，没被认成同一条，11-04 的邮件里提醒了两次
- 修法见 §4「去重和提醒的规则」；用真实库重放，42 条命中记录里只有这一条从「不重复」变成「重复」

### 2026-11-04 20:35 借鉴 Scrapling 的三项改进

- 用户让看 https://github.com/d4vinci/Scrapling。它的核心卖点是绕过反爬（指纹伪装、过 Cloudflare 等），和 §6 第 1 条冲突，**不迁移**；只借鉴三个思路，用户要求三个都做：
  1. 被限流自动放慢：429/503 按 Retry-After 等待（超过 120 秒直接放弃），并把该域名请求间隔加倍，最多 60 秒
  2. robots.txt 在代码里强制检查：14 个来源全部通过，只有综合采购有规则（其 /search/ 在授权例外里）
  3. 录制真实响应做回放测试：13 个来源录了样本（共 1.7MB），申通地铁除外
- 同时：中国政府采购网在沪中央单位搜索改为最多 6 页，被限流时保住前面保险搜索的结果
- 录制那次 dry-run 14 个来源全部正常，上海政府采购网有 1 条新保险命中，下次正式运行会发出；115 个测试通过

### 2026-11-04 11:36 定时任务改为每天三次

- 用户要求：早上 10:00 第一次、下午 15:00 第二次、晚上 22:00 第三次
- `install-task.ps1` 已改成三个每日触发器并重新注册；核对通过，下次运行 2026-11-04 15:00
- 一天三次后，综合采购两次运行之间的新增量（约 30–50 条）一般不会顶到 100 条上限，漏报概率明显降低
- 101 个测试通过（本次只改脚本和文档）

### 2026-10-31 09:01 接入申通地铁来源（用户授权忽略 robots）

- 白天复测发现：首页公告公开可见，但数据接口要求 `sign` 头（SHA256withRSA，私钥写在前端 JS 里），直接请求返回「签名错误：签名为null」
- **按用户 2026-10-31 的明确要求接入**，但没有复刻签名，而是用本机 Chrome 无头模式渲染公开首页（`--dump-dom`），
  从渲染后的 DOM 读 8 个栏目的最新 10 条（招标公告、单一来源公示、竞价公告、中标候选人/结果、揭榜询比等）
- 局限：每栏目只有最新 10 条、没有历史搜索；没有详情链接，邮件链接指向平台首页；需要本机装有 Chrome/Edge
- 真实 dry-run：解析出 69 条，近 30 天保险类命中 0 条；78 个测试通过
- robots 例外和「不复刻签名」两条已写进 CLAUDE.md 和 HANDOFF §6

### 2026-10-31 08:48 申通地铁平台复测

- 白天运营时段（7:00–19:00）访问 https://eps.shmetro.com/ ：返回 200，是「MSCP系统」单页应用
- robots.txt 为全站禁止：`User-agent: *` / `Disallow: /`（昨晚非运营时段 robots 返回 404，没查到规则）
- 按项目硬性规则「robots.txt 禁止的路径必须先问用户」，这里整站禁止，直接判定**不接**，没有继续抓列表页
- 申通系的保险公告已部分被综合采购覆盖（磁浮员工综合福利保险、维保公司机动车保险、运营财产险顾问等）

### 2026-10-31 00:11 接入第三批两个来源

- 新增 `airport`（上海机场集团·阳光采购）和 `chengtou`（上海城投集团·招采服务平台）：都是公开 JSON 接口、免登录；
  机场先 GET 列表页拿加速乐 Cookie 再调接口，城投按 size=100 全量翻页、翻到回看窗口之前就停
- 两家都没有可用的独立详情页（机场详情页对新公告 404/暂无数据，城投是弹窗），邮件链接指向公告列表页
- `config.toml` 关键词新增「一切险」，接住「建筑安装工程一切险」（机场 10-19 的项目就是这类标题）
- 真实 dry-run：机场抓到 217 条、命中 1 条（浦东机场运行保障基地建安工程一切险 10-19，命中「一切险」）；城投抓到 200 条、命中 0 条
- 71 个测试通过；下次 10:00 定时运行会把这个机场项目发出来

### 2026-10-30 23:19 首次正式发信（配置邮箱后）

- 23:19 测试邮件发送成功
- 23:20–23:22 首次正式运行：11 个来源中 10 个正常；共写入 27 条公告，6 条跨站/同站重复自动合并，**21 条汇总成一封邮件发出**（7 条综合采购历史补抓 + 14 条本轮新命中），数据库待提醒清零
- 邮件主题「上海保险招标提醒：21 条新公告」，用户确认已收到（收件箱）
- 国泰海通抓取失败：TLS 握手被服务器直接重置（Python、curl 及直连网络都试过）；当天 21:50 它还正常抓到 30 条。连续失败 2 次，未达告警阈值
- 首次发信前的数据库快照：`tender-alert-backups/tender-alert-data-20261030-231917-before-first-mail.db`
- 定时任务已注册：每天 10:00 运行一次，用 `Get-ScheduledTask -TaskName TenderAlert` 查看

### 2026-10-30 23:30–24:00 第三批来源调研

- 先查已有覆盖：国企采购近一年标题/正文检索，六家全部 0 条（仅「上港」正文出现 1 次）；综合采购最新 100 条采购公告里上海机场 5 条、申通系（上海地铁/轨道交通）10 条，上港/城投/电气/申能 0 条
- 六家自有门户逐一探测（合规：每站 ≤15 个请求、间隔 ≥2 秒、不碰验证码/登录）：
  机场集团、城投集团值得接入；申通地铁需白天复测；上港、申能不接；上海电气、城投水务先观察
- 详细依据、接口地址和字段见 `采购门户调研.md` 的「第三批调研结果」

### 2026-10-30 各来源真实 dry-run 的结果（回看 30 天）

| 来源 | 结果 |
|---|---|
| zfcg_sh / shggzy / ccgp | 正常，详见早先运行记录 |
| shggzy_zhcg | 一次性补抓用了 9 次搜索请求，找到 15 条，其中 7 条进入待提醒；之后每次运行每个栏目只看第 1 页 |
| ceair | 只发 2 次请求，没有触发限流；抓到 52 条，命中「2026-2027年度飞机维修人责任险」（平台上有 3 条重复记录，所以才把去重改成同来源内也合并）；外地分公司正确过滤 |
| bosc | 抓到 64 条，最近 30 天没有保险类（之前的董监高责任险、员工补充医疗保险都早于 30 天） |
| gtht | 抓到 30 条，最近 30 天没有保险类（这家集中在 3 月采购） |
| baowu | 4 次标题搜索共找到 7 条近期含「险」的公告，都是风险作业、个险等无关项；上海保险项目 0 条 |
| cosco | 近 30 天共 295 条，分 3 页；上海地区公告正常识别，但标题命中保险关键词的项目 0 条 |
| srcb | 2026-10-30 复核：3 次请求，抓到 30 条，命中「上海农商银行董事及高级管理人员责任险采购项目供应商征集公告」（10-20） |
| ebscn | 2026-10-30 复核：1 次搜索加 3 次详情，命中 3 条保险项目（员工补充医疗、董监高责任险等），都早于 30 天的回看范围。去掉 `dealMode` 筛选后结果完全相同（都是 12 条），说明只查公开招标不会漏 |

## 10. 变更记录

每次改动后在这里**追加**一条，最新的放在最下面。格式：日期 · 谁 · 改了哪些文件 · 改了什么、为什么 · 测试结果。

- 2026-10-30 · 最初开发会话 · 全部文件 · 项目从零建成，含 7 个来源；交接快照 `tender-alert-backups/tender-alert-20261030.zip` · 49 个测试全部通过
- 2026-10-30 · 最初开发会话 · CLAUDE.md、HANDOFF.md · 安装 git 并建立仓库（首个提交），写明提交规则：开工前查看 git status/log，每项改动测试通过后提交，并在这里登记 · 49 个测试全部通过
- 2026-10-30 · Codex · `tender_alert/sources/srcb.py`、`tests/test_new_enterprise_sources.py`、`config.toml`、`README.md`、`HANDOFF.md` · 接入上海农商银行供应商门户，静态分页按上次位置增量抓取 · 52 个测试全部通过
- 2026-10-30 · Codex · `tender_alert/sources/ebscn.py`、`tests/test_new_enterprise_sources.py`、`config.toml`、`README.md`、`HANDOFF.md` · 接入光大证券集中采购平台，公开搜索命中后再读取详情日期并缓存 · 55 个测试全部通过
- 2026-10-30 · Codex · `tender_alert/sources/baowu.py`、`tests/test_new_enterprise_sources.py`、`config.toml`、`README.md`、`HANDOFF.md` · 接入中国宝武宝华招标平台，按日期检索保险类标题并利用招标人、项目地点过滤上海项目 · 58 个测试全部通过
- 2026-10-30 · Codex · `tender_alert/sources/cosco.py`、`tests/test_new_enterprise_sources.py`、`config.toml`、`README.md`、`HANDOFF.md` · 接入中远海运集团采购信息系统，按公开接口省市字段筛选上海并只匹配标题 · 61 个测试全部通过
- 2026-10-30 · Codex · `采购门户调研.md`、`HANDOFF.md` · 更新已接入公司和剩余候选门户的状态，避免调研文档与代码不一致 · 文档更新，61 个测试保持通过
- 2026-10-30 · Claude（复核） · `HANDOFF.md` · 审查 srcb/ebscn/baowu/cosco 四个来源，没发现问题；补充 srcb、ebscn 的真实 dry-run 结果 · 61 个测试全部通过
- 2026-10-30 · Claude Code（Claude Opus 5.5） · `CLAUDE.md` · 规定提交说明最后一行用 `Agent:` 署名，方便在 git 记录里直接看出是哪个 agent 改的；在此之前的提交没有署名，要看这里的记录区分 · 文档改动，未涉及代码
- 2026-10-30 · DeepSeek Harness（deepseek-flash） · `install-task.ps1`、`README.md`、`需求.md`、`HANDOFF.md` · 定时任务从「工作日 5 次」改为「每天 10:00 一次」（用户确认），并同步更新综合采购漏报风险的说明 · 61 个测试通过
- 2026-10-30 · DeepSeek Harness（deepseek-flash） · `HANDOFF.md` · 登记邮箱配置完成、首次正式发信结果（21 条）、定时任务注册和国泰海通连续失败的情况，更新待办 · 文档改动，61 个测试保持通过
- 2026-10-30 · DeepSeek Harness（deepseek-flash） · `采购门户调研.md`、`HANDOFF.md` · 完成第三批六个候选门户调研：机场集团、城投集团建议接入，申通地铁待白天复测，上港/申能不接，电气/城投水务先观察；登记接口地址和判断依据 · 文档改动，61 个测试保持通过
- 2026-10-31 · DeepSeek Harness（deepseek-flash） · `tender_alert/sources/airport.py`、`tender_alert/sources/chengtou.py`、`tests/test_airport_source.py`、`tests/test_chengtou_source.py`、`config.toml`、`README.md`、`采购门户调研.md`、`HANDOFF.md` · 接入上海机场集团、上海城投集团两个来源（公开 JSON 接口），关键词新增「一切险」 · 71 个测试全部通过
- 2026-10-31 · DeepSeek Harness（deepseek-flash） · `HANDOFF.md` · 待办分工：国泰海通后续交给 Claude 跟进；申通地铁排在 2026-10-31 早 7 点后复测 · 文档改动，71 个测试保持通过
- 2026-10-31 · Claude Code（Claude Opus 5.5） · `tender_alert/sources/airport.py`、`tests/test_airport_source.py`、`HANDOFF.md` · 复核 DeepSeek 的 5 个提交（通过）；机场公告不再一律标成「招标」，改为按标题归类（中标结果公示不再当招标提醒）；更新 HANDOFF 旧信息；登记国泰海通排查结论和礼貌规则的教训。定时任务按用户要求保持每天一次 · 72 个测试全部通过
- 2026-10-31 · Claude Code（Claude Opus 5.5） · `HANDOFF.md` · 更正国泰海通的结论：用户用手机 5G 也打不开门户，是网站本身故障，不是本机 IP 被封 · 文档改动，72 个测试保持通过
- 2026-10-31 · DeepSeek Harness（deepseek-flash） · `HANDOFF.md`、`采购门户调研.md` · 申通地铁复测结论：白天可访问但 robots.txt 全站禁止，判定不接；更新待办和调研文档 · 文档改动，72 个测试保持通过
- 2026-10-31 · DeepSeek Harness（deepseek-flash） · `tender_alert/sources/shmetro.py`、`tests/test_shmetro_source.py`、`config.toml`、`README.md`、`CLAUDE.md`、`采购门户调研.md`、`HANDOFF.md` · 按用户授权接入申通地铁来源：无头浏览器渲染公开首页，不复刻接口签名；robots 例外写进规则 · 78 个测试全部通过
- 2026-10-31 · Claude Code（Claude Opus 5.5） · `HANDOFF.md` · 用户要求正式运行并发信：14 个来源中 13 个正常，发出 2 条（上海国投员工商业保险重新招标 10-31、浦东机场建安工程一切险 10-19）；国泰海通门户已恢复；申通地铁 19:06 失败，原因是超出网站 7:00–19:00 开放时间 · 72 个测试保持通过
- 2026-11-02 · Claude Code（Claude Opus 5.5） · `tender_alert/http.py`、`tender_alert/__main__.py`、`tender_alert/sources/shmetro.py`、`tests/test_network.py`、`tests/test_shmetro_source.py`、`HANDOFF.md` · 用户确认申通地铁的 robots 例外确实是本人授权；申通在 7:00–19:00 以外跳过、不算失败；运行前先等网络就绪（开机补跑时网络没好会让所有来源各记一次失败） · 81 个测试全部通过
- 2026-11-02 · Claude Code（Claude Opus 5.5） · `tender_alert/export.py`、`tender_alert/__main__.py`、`tests/test_export.py`、`.gitignore`、`README.md`、`HANDOFF.md` · 用户要求把历史筛中结果存成可读文件：每次正式运行后自动更新 `exports/历史命中公告.csv`（UTF-8 BOM，Excel 直接打开；文件被 Excel 占用时只记警告），新增 `--export-json` 手动导出 JSON · 85 个测试全部通过
- 2026-11-02 · Claude Code（Claude Opus 5.5） · `tender_alert/store.py`、`tender_alert/config.py`、`tender_alert/filters.py`、`tender_alert/__main__.py`、`tender_alert/sources/{cosco,gtht,shggzy_zhcg,zfcg_sh}.py`、`config.toml`、`install-task.ps1`、测试、文档 · 用户要求存下所有上海招投标公告：新增 archive 表和 `[archive]` 开关；开启后按类型抓取的来源抓全部类型；中远海运不再在模块内按关键词筛；定时任务改为每天 10:00 和 20:00 并已重新注册；全量 dry-run 一轮存档 902 条、用时约 2.5 分钟。第二步（把只搜「险」的来源改成全量）见 §8 · 88 个测试全部通过
- 2026-11-02 · Claude Code（Claude Opus 5.5） · `tender_alert/{__main__,store}.py`、`tender_alert/sources/{__init__,zfcg_sh,shggzy,gtht,airport,ceair,ebscn,baowu,ccgp}.py`、`tests/test_full_lists.py`、`HANDOFF.md`、`README.md` · 全量存档第二步：新增增量抓取（INCREMENTAL），8 个只搜「险」的来源在存档模式下都改为抓全量（各自抓法见 §5「全量存档模式」）；全量 dry-run 一轮存档 1793 条、76 个请求、约 3.5 分钟，保险提醒不受影响 · 96 个测试全部通过
- 2026-11-02 · Claude Code（Claude Opus 5.5） · `tender_alert/export.py`、`tender_alert/__main__.py`、`tests/test_export.py`、`README.md`、`HANDOFF.md` · 用户要求给全量存档也加 CSV：按发布月份拆分到 exports/全量存档/YYYY-MM.csv（量大，单文件 Excel 打不动），每次正式运行后只重写本次有变动的月份；新增 --export-archive 全部重导；存档表还不存在时安静跳过。用真实数据库副本加城投数据验证过 · 99 个测试全部通过
- 2026-11-02 · Claude Code（Claude Opus 5.5） · `tender_alert/__main__.py`、`tender_alert/mailer.py`、`tests/test_core.py`、`HANDOFF.md`、`需求.md` · 用户要求每次运行都发邮件：没有新保险公告时也发运行报告，邮件末尾附各来源的运行情况表（抓到/新命中/新存档，失败的标红写原因） · 101 个测试全部通过
- 2026-11-04 · DeepSeek Harness（deepseek-flash） · `install-task.ps1`、`README.md`、`需求.md`、`HANDOFF.md` · 定时任务改为每天 10:00 / 15:00 / 22:00 三次（用户确认），已重新注册并核对三个触发器、下次 15:00 · 文档改动，101 个测试保持通过
- 2026-11-04 · Claude Code（Claude Opus 5.5） · `tender_alert/http.py`、`tests/test_network.py` · 借鉴 Scrapling：429/503 按 Retry-After 等待、按域名把请求间隔加倍（最多 60 秒），Retry-After 超过 120 秒直接报错下次再试 · 测试全部通过
- 2026-11-04 · Claude Code（Claude Opus 5.5） · `tender_alert/robots.py`、`tender_alert/http.py`、`tender_alert/__main__.py`、`tender_alert/sources/ccgp.py`、`tests/test_robots.py`、`tests/test_full_lists.py`、`CLAUDE.md` · robots.txt 在代码里强制检查，用户授权的综合采购 /search/ 和申通地铁两个例外列进白名单；ccgp 在沪中央单位搜索最多 6 页，限流时保留保险搜索结果 · 114 个测试全部通过
- 2026-11-04 · Claude Code（Claude Opus 5.5） · `tender_alert/recording.py`、`tender_alert/http.py`、`tender_alert/__main__.py`、`tests/replay.py`、`tests/test_recorded_fixtures.py`、`tests/fixtures/`、`README.md`、`HANDOFF.md` · 新增 `--dry-run --record` 录制各来源真实响应，测试不联网回放检查解析；录了 13 个来源 · 115 个测试全部通过
- 2026-11-04 · Claude Code（Claude Opus 5.5） · `tender_alert/store.py`、`tests/test_core.py`、`HANDOFF.md` · 跨站去重只比项目名称（去掉末尾公告类型字样）并要求 kind 相同；同站或 kind 不明时仍要完整标题相同；已有记录启动时重算 norm_title。起因是申通和综合采购同一条保险招标被提醒两次 · 121 个测试全部通过
- 2026-11-05 · Claude Code（Claude Opus 5.5） · `install-task.ps1`、`README.md`、`HANDOFF.md` · 用户要求：定时任务打开唤醒电脑运行并已重新注册；Windows「允许使用唤醒定时器」当前禁用，需用户自己启用才生效 · 脚本和文档改动，121 个测试保持通过
- 2026-11-05 · Claude Code（Claude Opus 5.5） · `HANDOFF.md` · 登记用户已启用插电时的唤醒定时器，实际唤醒待验证 · 文档改动，121 个测试保持通过
- 2026-11-08 · Claude Code（Claude Opus 5.5） · `.env`（不进 git）、`HANDOFF.md` · 扩展 `.env` 支持多收件人配置并核对读取 · 文档改动，测试不受影响
- 2026-11-11 · Claude Code（Claude Fable 5.2） · `采购门户调研.md`、`HANDOFF.md` · 第四批来源调研：30 多个候选站各做公开页面探测并和存档比对，分成建议接、可接、不接三类；没有改代码 · 文档改动，测试不受影响
- 2026-11-11 · Claude Code（Claude Fable 5.2） · `tender_alert/sources/{sse,cfcpn}.py`、`tests/test_finance_sources.py`、`tests/fixtures/{sse,cfcpn}.json.gz`、`config.toml`、`README.md`、`HANDOFF.md`、`采购门户调研.md` · 接入上交所采购门户和金采网两个来源并启用，排在各机构自有门户之后、中国政府采购网之前 · 129 个测试全部通过
- 2026-11-11 · Claude Code（Claude Fable 5.2） · `tender_alert/sources/edu.py`、`tests/test_edu_source.py`、`tests/fixtures/edu.json.gz`、`config.toml`、`README.md`、`HANDOFF.md`、`采购门户调研.md` · 接入上海 26 所高校的采购网站，合成一个来源 edu，用通用列表解析器；已启用 · 141 个测试全部通过
- 2026-11-11 · DeepSeek Harness（deepseek-flash） · `需求.md`、`HANDOFF.md` · 把第五批来源调研（金融基础设施/传媒/医院自有站/中科院/央企在沪/市属剩余/代理聚合）整理进需求文档 §12，含实证线索、优先级、覆盖度比对和合规注意；待办登记交给 Claude 逐步实现。本次只改文档，未动代码 · 文档改动，本地 144 个测试通过
- 2026-11-11 · Claude Code（Claude Fable 5.2） · `tender_alert/sources/edu.py`、`tests/test_edu_source.py`、`tests/fixtures/edu.json.gz`、`config.toml`、`README.md`、`HANDOFF.md`、`采购门户调研.md` · 高校来源补接上海交大、上海工程技术大学（采购平台公开列表接口）和上海海事大学（日期从文章地址取），共 29 所 · 144 个测试全部通过
- 2026-11-11 · Claude Code（Claude Fable 5.2） · `tender_alert/sources/cfcpn.py`、`tests/test_finance_sources.py`、`tests/fixtures/cfcpn.json.gz` · 金采网有频率限制（连续六七个请求后 403）：每种查法最多 3 页、间隔 5 秒，中途被限流保留已拿到的 · 测试全部通过
- 2026-11-11 · Claude Code（Claude Fable 5.2） · `tender_alert/{__main__,config,mailer}.py`、`tests/test_core.py`、`config.toml`、`README.md`、`HANDOFF.md` · 用户要求按更新快慢分频率：新增 `once_a_day` 名单，11 个更新慢的来源当天成功过就跳过，邮件运行情况里标出 · 151 个测试全部通过
- 2026-11-11 · Claude Code（Claude Fable 5.2） · `config.toml`、`README.md`、`HANDOFF.md`、`采购门户调研.md` · 用户要求停用金采网（cfcpn）：从 enabled 里去掉，代码保留 · 配置和文档改动，测试保持通过
- 2026-11-18 · Claude Code（Claude Opus 5.5） · `tender_alert/mailer.py`、`tests/test_core.py`、`HANDOFF.md`、`README.md` · 用户嫌邮件太长：「本次运行情况」不再逐个罗列来源，只留一行汇总，失败的来源单独列出原因 · 151 个测试全部通过
- 2026-11-18 · DeepSeek Harness（deepseek-flash） · `tender_alert/sources/{orgs,edu}.py`、`tests/test_orgs_source.py`、`tests/fixtures/orgs.json.gz`、`config.toml`、`README.md`、`需求.md`、`HANDOFF.md` · 接入第五批来源 `orgs`（首批 12 家：SMG、肿瘤/华山医院、中科院三所、城银清算、CIPS、中银国际证券、上海国际信托、机电设备招标、中烟上海），并登记农信银、上海科技馆与央企/市属集团 P3 复测全部不接的结论（上午提交漏追加本行，此处补齐） · 158 个测试全部通过
- 2026-11-18 · Antigravity（Gemini 4 Carbon） · `config.toml`、`tender_alert/models.py`、`tender_alert/sources/shmetro.py`、`tests/{test_core,test_shmetro_source}.py`、`HANDOFF.md` · 核查历史 58 条命中与 1.76 万条存档：关键词补「综合险」「医责险」「安责险」「董责险」等简称（挽回「残疾人阳光综合险」公开招标漏报），排除词补「虚拟仿真」（高校保险教学软件）与「医疗保障信息平台」（医保中心 IT 运维）；`classify_kind` 把「失败」归入 result，申通「招标公告」栏内的变更/延期公告按标题归入 correction 以修复跨站去重 · 158 个测试全部通过
- 2026-11-18 · Antigravity（Gemini 4 Carbon） · `tender_alert/sources/{orgs,edu}.py`、`tests/test_orgs_source.py`、`tests/fixtures/orgs.json.gz`、`config.toml`、`README.md`、`需求.md`、`采购门户调研.md`、`HANDOFF.md` · 扩展 `orgs` 来源（12 → 21 家）：新增上海国际招标（SITC）、上海中世建设咨询、上海财瑞、上海百通 4 家头部代理（并将 `shbid` 升级为招标+中标双栏目及外地过滤），以及中国银联、上海清算所、上期所、上金所、上海地产集团；`edu.parse_list` 兼容上金所两位年份日期与清算所 TRS 文章路径日期。真实 dry-run 录得 30 个响应、21 家全通，抓到 238 条、命中 3 条（银联员工团体保险采购+更正、SMG 车辆保险） · 160 个测试全部通过
- 2026-11-18 · Antigravity（Gemini 4 Carbon） · `HANDOFF.md` · 同步交接文档顶部日期、§2 状态表（18 来源启用 17 个、160 测试、数据库 74/58 条与存档 17,624 条）、§4 代码结构、§5 `orgs` 来源说明、§8 待办、§9 最近操作，并补记上午漏登记的变更记录 · 文档改动，160 个测试保持通过
- 2026-11-18 · Antigravity（Gemini 4 Carbon） · `tender_alert/{prior,store,mailer,export,__main__}.py`、`tests/test_prior.py`、`README.md`、`HANDOFF.md` · 新增往年同标续标核查与曾经中标公司标记功能：识别并跳过一次性工程险（标为 `one_off`），对可续保项目回查近 3 年本地库、上海政采、国企采购及各机构自有公开列表，提取曾经中标公司（或第一候选人）在邮件标题下方高亮展示并同步导出至历史 CSV · 170 个测试全部通过
- 2026-11-18 · Antigravity（Gemini 4 Carbon） · `tender_alert/prior.py`、`tests/test_prior.py`、`HANDOFF.md` · 往年中标人提取兼容多行表头（首行 colspan 标题行）与政采合同公告的「乙方（供应商）」字段 · 170 个测试全部通过
- 2026-11-18 · Antigravity（Gemini 4 Carbon） · `tender_alert/prior.py`、`HANDOFF.md` · 往年核查补充上交所历史静态分页（`s_list_1..5`）及在沪中央单位的中国政府采购网低频兜底检索 · 170 个测试全部通过
- 2026-11-18 · Antigravity（Gemini 4 Carbon） · `tender_alert/prior.py`、`HANDOFF.md` · 往年核查提取前解码 `&nbsp;` 等 HTML 实体，并兼容采购单位跨年更名（如计生中心更名家庭发展中心） · 170 个测试全部通过
- 2026-11-18 · Antigravity（Gemini 4 Carbon） · `tender_alert/prior.py`、`HANDOFF.md` · 政采全文兜底改用单位简称+保险，并剥离公司名前粘连的带括号项目序号 · 170 个测试全部通过
- 2026-11-18 · DeepSeek Harness（Gemini 4 Carbon） · `tender_alert/{__main__,export}.py`、`tender_alert/sources/{__init__,orgs,edu}.py`、`tests/test_orgs_source.py`、`HANDOFF.md` · 子单位独立健康追踪：`orgs`（21 家）与 `edu`（29 所）单个单位连续失败达到阈值时独立触发邮件「数据来源异常」告警，避免少于 1/3 子站长期失效时被静默吞掉 · 171 个测试全部通过
- 2026-11-18 · DeepSeek Harness（Gemini 4 Carbon） · `tender_alert/sources/ccgp.py`、`tests/test_full_lists.py`、`HANDOFF.md` · 修复中国政府采购网增量水位：改为 `STATEFUL` 分别记录保险搜索（固定保持 7 天回看窗口）与在沪中央单位全量搜索（跨天补前一日、同日后续轮次只查当天，避免晚间两日叠加超 6 页截断；遇限流不推进水位以便下轮自动补抓） · 172 个测试全部通过
- 2026-11-18 · DeepSeek Harness（Gemini 4 Carbon） · `tender_alert/{store,__main__,prior}.py`、`tests/test_core.py`、`HANDOFF.md` · 优化存储与查询开销：按来源使用 `store.batch()` 合并单次事务提交（免除单轮数千次独立刷盘）；`store.archive()` 仅在字段真有变化时才更新记录与 `last_seen` 并为 `last_seen` 建索引（避免历史月份 CSV 每次被无差别重写）；`--dry-run` 内存库只拷贝小表及近 90 天/保险存档行；`prior` 单轮缓存本地保险历史查询；`store.add()` 跨站重复返回 `False` 修正「新命中」日志计数 · 173 个测试全部通过
- 2026-11-18 · DeepSeek Harness（Gemini 4 Carbon） · `tender_alert/sources/{_common,edu,orgs}.py`、`tender_alert/prior.py`、`tests/test_orgs_source.py`、`HANDOFF.md` · 优化大网页解码与超长历史页解析：新增 `decode_html` 优先读取前 4KB `<meta charset>` 或 UTF-8 快速解码（避免对 SMG 2.8MB 全历史页跑全文编码嗅探）；`edu.parse_list` 支持 `stop_before` 连续 40 条超期提前终止（兼容顶部少量置顶旧公告） · 174 个测试全部通过
- 2026-11-18 · DeepSeek Harness（Gemini 4 Carbon） · `tender_alert/__main__.py`、`install-task.ps1`、`tests/test_core.py`、`HANDOFF.md` · 运行期稳健性优化：正式运行增加 `data/run.lock` 单实例文件互斥锁（防手动运行与后台定时任务撞车锁库或重发邮件）；日志改为 `RotatingFileHandler`（5MB × 3 份自动轮转）；`previews/` 自动只保留最近 20 个预览网页；`install-task.ps1` 超时上限从 20 分钟放宽到 45 分钟，并同步更新交接文档状态表 · 176 个测试全部通过
- 2026-11-19 · DeepSeek Harness（Gemini 4 Neon） · `tender_alert/mailer.py`、`tests/test_core.py`、`HANDOFF.md` · 统一邮件公告标题下方详情行（「预算/截止/命中」）字体大小为 `12px` 并补齐 `margin-top:4px`，与上方「往年核查」行保持一致 · 177 个测试全部通过
- 2026-11-19 · DeepSeek Harness（Gemini 4 Neon） · `tender_alert/mailer.py`、`tests/test_core.py`、`HANDOFF.md` · 按用户要求精简邮件页脚文案，删去「关键词等设置在 config.toml 里修改。」，仅保留「由 tender-alert 自动发送。」 · 177 个测试全部通过
- 2026-11-19 · DeepSeek Harness（Gemini 4 Neon） · `CLAUDE.md`、`HANDOFF.md`、`需求.md`、`config.toml` · 公开推送前脱敏：移除文档中的本机绝对路径、个人邮箱及业务规划说明和历史排查记录中的真实 IP，并更新远程仓库状态说明 · 文档与配置注释改动，177 个测试全部通过
