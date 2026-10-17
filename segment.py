# -*- coding: utf-8 -*-
"""细分器 v2：按「业务对象」全局重切 + 付费意愿排序。

为什么要全局重切而不是在每个一级簇内切：
  「银行流水」在 x_table(90 帖) / x_money(18 帖) / x_text(13 帖) / x_doc(11 帖) 都出现，
  同一件事被动词+对象语义组拆散了。只在簇内切会永远看不到完整的市场。

付费意愿为什么拆成两个信号（JEV 实测发现的关键陷阱）：
  付费锚词里含 quickbooks/xero/subscription 这类**工具名**，而 r/QuickBooks 这个 sub
  的付费锚基线率高达 52.4%（人人都在谈 QuickBooks），全站基线只有 8.9%。
  所以必须区分：
    tool_mention  这个人本来就在为软件花钱 -> 预算存在，但很可能已被巨头占住
    pay_intent    I'd pay / too expensive / 找替代 / 想退订 -> 正在找替代，最可能转付费
  JEV 判定（choice conf 0.72）：pay_score = tool_mention 40% + pay_intent 60%
  JEV 判定（choice conf 0.91）：巨头占位**拆两类标注**，不简单加分或扣分
  JEV 判定（conf 0.36 灰区）：非工具类、同作者重复帖 -> **保留但强制标记**，交人工

同作者重复（JEV 灰区判定）：同一作者同一细分类只计 1 票，另算「重复强度」。
"""
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import radar  # noqa: E402
import remote_ai  # noqa: E402

# 合规敏感社区（从 subs.json 读，读不到用兜底）——用于警示标注，不做否决
try:
    _SUB_CFG = json.load(open(os.path.join(HERE, "config", "subs.json"), encoding="utf-8"))
    SENSITIVE_SUBS = _SUB_CFG.get("_compliance_sensitive_subs") or []
except OSError:
    SENSITIVE_SUBS = []

RAW = os.path.join(HERE, "working", "arctic_raw")
OUT_JSON = os.path.join(HERE, "working", "segments.json")
OUT_TXT = os.path.join(HERE, "review", "_segment_rates.txt")

DEMAND_CATS = {"ask_tool", "complain", "workaround", "repeating"}

# ---------------------------------------------------------------- 业务对象
# 顺序即优先级：先匹配更具体的（bank_stmt 在 invoice 前面）。
BIZ_OBJECT = [
    ("bank_stmt",   r"bank statement|bank transaction|bank line|bank feed|statement|ach\b|wire transfer|"
                    r"credit card statement|reconcile"),
    ("reconcile",   r"reconcil|match(ing)? (the )?(transaction|balance|entry|line)|tie[ -]?out|unmatched|"
                    r"vlookup|xlookup|index[ .]match"),
    ("invoice",     r"invoice|invoicing|bill\b|billing|receipt"),
    ("payroll",     r"payroll|pay ?roll|wage|salary|timesheet|time ?sheet|contractor pay|1099|w-?2\b"),
    ("tax",         r"\btax\b|taxes|tax season|vat\b|gst\b|sales tax|estimated tax"),
    ("csv_file",    r"\bcsv\b|\btsv\b|export file|data export|raw data|import file|flat file"),
    ("ledger",      r"ledger|journal entr|journal|chart of account|general ledger|debit|credit"),
    ("ap_ar",       r"\bap\b|\bar\b|accounts payable|accounts receivable|aging|overdue"),
    ("inventory",   r"inventory|stock level|sku\b|on hand|reorder|units sold"),
    ("expense",     r"expense|spend(ing)?|categor(y|ize) (my |the )?(transaction|expense)|receipt scan"),
    ("budget",      r"budget|cash ?flow|forecast|burn rate|spend report|spending plan"),
    ("report",      r"report|dashboard|\bpivot\b|kpi|summary|chart of (my|the)"),
    ("formula",     r"formula|function|\bvba\b|macro|power query|array formula|#REF|#VALUE|#N/A"),
    ("format",      r"format(ting)?|conditional format|alignment|column width|style|wrap text"),
    ("email_doc",   r"email|outlook|gmail|inbox|newsletter"),
    ("pdf",         r"\bpdf\b|scan(ning|ned)? (my |the )?(receipt|invoice|doc)|ocr"),
    ("photo",       r"photo|image|picture|raw file|lightroom|\blr\b"),
    ("video",       r"video|footage|premiere|final cut|resolve|davinci|edit(ing)? (the )?(video|footage)"),
    ("subtitle",    r"subtitle|caption|\bsrt\b|transcript"),
    ("3dprint",     r"3d print|3dprint|prusa|ender|print bed|filament|slicer"),
    # ⚠️ v1.1.7 修：`\bvia\b` 会匹配英文介词 "via"（Why do you say via instead of through?），
    # 导致 r/Notion 里"via 报错"的帖子被误归为「机械键盘改键」。改为只认 QMK 语境。
    ("keyboard",    r"keyboard|keycap|switch(es)?\b|\bqmk\b|\bvia (json|qmk|config|keymap)|"
                    r"remap|lub(ing|ed)?\b|keychron|firmware.*(board|key)"),
    ("fishing",     r"fish(ing)?|bait|lure|angler|reel|\brod\b|tackle"),
    ("game",        r"board ?game|game (night|play)|d&d|dnd|catan|meeple|expansion|rulebook"),
    ("model",       r"scale model|model (kit|railway|aircraft|ship)|miniature|airbrush"),
    ("diy",         r"\bdiy\b|home improvement|renovation|deck|fence|garage|toolbox|wood ?work"),
    ("sysadmin",    r"backup|monitor(ing)?|alert(ing)?|uptime|log ?file|incident|certificate|vm\b|docker|kubernetes"),
    ("api_data",    r"\bapi\b|json|endpoint|webhook|scrap(e|ing)|integrat(e|ion|ing)|sync(ing)? .*(to|with)"),
    # ---- v2 新增：诊断「未归类」时发现的真需求 ----
    # 追客户要资料：Bookkeeping 反复出现（"how do you handle clients who go quiet on
    # document requests?" / "biggest hassle with getting documents from clients"）
    ("chase_docs",  r"(chasing|chase|nag(ging)?|hassle|struggl(e|ing)) (my |the |for )?(client|customer)s?[^.]{0,30}"
                    r"(document|doc|info|information|data)|client(s)? (go|went) quiet|"
                    r"getting (documents|info) from clients?|collect(ing)? (the )?(documents|receipts|info) from|"
                    r"remind(ing)? clients? to (send|provide)|missing (documents|receipts) from client"),
    # 报价 / 需求蔓延：freelance 连续多帖（scope creep / fixed price vs day rate）
    ("scope_quote", r"scope creep|scope change|change order|fixed price|day rate|hourly rate|"
                    r"(my |the )?rate(s)?|quote(s|d|ing)? (a|my|the|for)|pricing|undercharge|"
                    r"charge (more|less)|bill (more|less)|estimate (is|was) (wrong|off)"),
    # 演练/准备材料：consulting 反复出现（tabletop exercise prep）
    ("prep_material", r"tabletop|exercise prep|prep (time|material)|run ?book|playbook|"
                      r"scenario(s)? (prep|template)|workshop (prep|material)|"
                      r"training material|course material|curriculum|handout"),
    # ================= v1.1.7 新增：非财务领域（远程可做 + AI 可做）=================
    # 下面这些全部按「远程可做 + AI 可做」标准挑：不需要线下、不需要现场、不需要手工。
    ("seo_content",   r"keyword research|serp|on[- ]page seo|backlink|meta description|"
                      r"content (calendar|plan|cluster)|blog post|newsletter|copywriting|"
                      r"ghostwrit|product description|landing page"),
    ("ads_campaign",  r"ad (spend|campaign|copy|creative|set)|facebook ad|google ad|"
                      r"ppc\b|cpc\b|roas|retargeting|audience (target|segment)|"
                      r"email (list|campaign|blast|sequence|open rate)|mailchimp|klaviyo|"
                      r"social (media|post) (schedule|calendar)|content scheduler"),
    ("ecom_ops",      r"shopify|woocommerce|amazon (seller|listing|fees)|etsy|"
                      r"product (photo|listing|variation)|inventory (sync|level)|"
                      r"order (fulfil|track)|shipping label|return(s)? (process|policy)|"
                      r"supplier|alibaba|print on demand|printondemand|fulfil+ment"),
    ("social_content", r"youtube (channel|thumbnail|title|seo)|tiktok|instagram reel|"
                       r"shorts|thumbnail|caption|transcri(be|pt)|auto[- ]?subtitle|"
                       r"video (edit|export|render|compress)|audio (clean|convert|compress)|"
                       r"podcast (edit|transcri|show)|newsletter (send|schedule)"),
    ("crm_sales",     r"crm\b|hubspot|salesforce|pipedrive|lead (gen|list|source)|"
                      r"cold (email|outreach)|prospect|follow[- ]up|quote (sent|signed)|"
                      r"proposal (write|send)|contract (sign|send)|onboard(ing)? client"),
    ("hr_ops",        r"onboard(ing)? (employee|new hire)|offboard|payroll run|"
                      r"timesheet|schedule (shift|rota)|recruit(er|ing)|job (post|description)|"
                      r"employee (handbook|survey)|performance review|training tracker"),
    ("customer_support", r"help ?desk|ticket (queue|system)|zendesk|intercom|live ?chat widget|"
                         r"faq (page|builder)|macro (library|template)|canned response|"
                         r"first response time|cs(at)?\b"),
    ("realestate_ops", r"listing (write|photo|description)|mls\b|showing (schedule|feedback)|"
                       r"open house|lead (response|follow)|rent (roll|screen|application)|"
                       r"tenant (screen|portal|communication)|lease (renew|abstract)|"
                       r"property (management|report)|hoa\b"),
    ("health_admin",  r"(book|manage|schedule) (a |the )?(appointment|visit)|"
                      r"(insurance|medical|billing) (claim|record|statement|reconcil)|"
                      r"patient (intake|form|record)|medical (bill|claim|coding)|"
                      r"copay|deductible|referral|prescription (refill|renew)|"
                      r"(health|fitness) (log|track|dashboard)|workout (log|plan)"),
    ("legal_admin",   r"contract (review|renew|clause)|nda\b|template (contract|agreement)|"
                      r"(legal|compliance) (document|checklist)|case (file|manage)|"
                      r"discovery|onboard(ing)? paper|notar(y|ize)|policy (document|draft)"),
    ("language_text", r"translat(e|ion|or)|locali[sz](e|ation)|proofread|copy ?edit(ing)?|"
                      r"transcri(be|ption)|subtitl(e|ing)|voice ?over|dubbing|"
                      r"machine translation|term(base|glossary)|interpret(er|ing)"),
    ("genealogy",     r"genealog(y|ical)|ancestor|dna (match|test)|family ?tree|"
                      r"census|vital (record|index)|archive|ship record|"
                      r"(family|ancestry) (history|record)"),
    ("data_pipeline", r"(data|etl) (pipeline|clean|transform)|api (call|integrat|key)|"
                      r"web ?hook|scrap(e|ing|er)|database (sync|migrat)|"
                      r"dashboard|report(ing)? (tool|automate)|spreadsheet (error|break)|"
                      r"formula|vba|macro|python script|power ?query"),
    ("devops_site",   r"docker|kubernetes|container|self[- ]host|homelab|vm\b|"
                      r"(server|backup|uptime|monitor|log) (setup|config|alert)|"
                      r"deploy|ci/cd|pipeline|nginx|ssl|certificate|dns\b"),
    ("design_asset",  r"(logo|brand|thumbnail|banner|icon|illustration|font) (design|need|file)|"
                      r"figma|canva|template (for|to)|mockup|wireframe|design system|"
                      r"color palette|layout (issue|problem)|photo edit(ing)?|batch edit"),
    ("home_admin",    r"(home|house|apartment) (maintenance|repair|inspect)|"
                      r"(landlord|tenant|rental) (task|agreement|inspection)|"
                      r"hoa|property (tax|insurance)|diy (guide|plan|project)|"
                      r"(moving|renovation|remodel) (checklist|plan|cost)|"
                      r"mortgage|refinanc|escrow|title search"),
]
BIZ_LIST = list(BIZ_OBJECT)

BIZ_CN = {
    "bank_stmt": "银行流水对账与整理", "reconcile": "对账匹配 / 查找引用", "invoice": "发票与账单处理",
    "payroll": "工资表与工时", "tax": "税务申报与计算", "csv_file": "CSV / 导出文件处理",
    "ledger": "分类账与凭证录入", "ap_ar": "应付应收与账龄", "inventory": "库存与 SKU 管理",
    "expense": "费用报销与分类", "budget": "预算与现金流", "report": "报表与汇总",
    "formula": "公式与 VBA 排错", "format": "表格格式与条件格式", "email_doc": "邮件批量处理",
    "pdf": "PDF 扫描与 OCR", "photo": "照片批处理", "video": "视频剪辑辅助",
    "subtitle": "字幕与转录", "3dprint": "3D 打印切片", "keyboard": "机械键盘改键",
    "fishing": "钓鱼装备记录", "game": "桌游组局与规则", "model": "模型制作",
    "diy": "家装 DIY 工具", "sysadmin": "系统运维监控", "api_data": "API 集成与同步",
    "chase_docs": "追客户要资料（催收凭证/信息）", "scope_quote": "报价与需求蔓延管理",
    "prep_material": "演练/培训材料准备",
    # ---- v1.1.7 非财务领域 ----
    "seo_content": "SEO 与内容生产", "ads_campaign": "广告投放与邮件营销",
    "ecom_ops": "电商运营（订单/库存/物流）", "social_content": "社媒与音视频内容处理",
    "crm_sales": "CRM 与销售跟进", "hr_ops": "HR 运营（入离职/排班/招聘）",
    "customer_support": "客服工单与知识库", "realestate_ops": "房产运营（租赁/带看/房源）",
    "health_admin": "健康与保险行政（预约/理赔）", "legal_admin": "法务文档管理（合同/合规）",
    "language_text": "翻译与文字校对", "genealogy": "家谱寻踪与档案",
    "data_pipeline": "数据管道与报表自动化", "devops_site": "自托管与运维",
    "design_asset": "设计素材批量处理", "home_admin": "房屋/租赁行政",
    "other": "（未归类）",
}

# ---------------------------------------------------------------- 付费信号（两类，JEV 判定）
# A. 工具名提及：预算存在，但巨头可能已占住
TOOL_MENTION = [
    ("qbo",    r"quickbooks|\bqbo\b|quick books"),
    ("erp2",   r"xero|net ?suite|myob|sage\b|\bwave\b|\badp\b|gusto|paychex|freshbooks|zoho"),
    ("office", r"microsoft 365|office 365|\bexcel\b|google sheets|airtable|notion\b"),
    ("adobe",  r"adobe|lightroom|capture one|\bpremiere\b|after ?effects|photoshop|davinci"),
    ("apps",   r"\bapp(s)?\b|\bsoftware\b|subscription|\bplan\b|licen[cs]e|per user|per seat"),
]
# B. 主动付费意愿：正在找替代 / 愿意多付 —— 新产品的真信号
PAY_INTENT = [
    ("will_pay",     r"i'?d pay|i would pay|happy to pay|would you pay|i'?ll pay|shill pay|"
                     r"i'?d buy|worth paying|worth it if|pay \$|budget for"),
    ("too_expensive", r"too expensive|pricey|cost too much|cheaper|\$\d+ ?/? ?(mo|month|yr|year)|"
                      r"out of budget|price (is|of|dropped|increase|went up)"),
    ("alt_seek",     r"alternative to|instead of|switch(ing)? (from|off)|migrat(e|ing) (from|off)|"
                     r"better than|replac(e|ing) (my|our|the)|why isn'?t there|"
                     r"no (good )?(app|tool|software|way) (for|that|to)|nothing (out there )?that"),
    ("cancel",       r"cancel(l?ing|led)? (my |our )?(subscription|plan|account)|refund|uninstall"),
]

# ---------------------------------------------------------------- 非工具类标记（JEV 灰区判定：保留但标记）
NON_TOOL = [
    ("cert_exam",  r"\b(cfe|cpa|cma|cima|aadmm|ciss|ccin|exam|certification|certified|accredited)\b"),
    ("job_hunt",   r"job hunting|looking for (a |my )?(job|work|internship)|resume|reference(s)?|"
                   r"interview|laid off|fired|career (advice|change|break|path)|switch career|"
                   r"escape the \d-\d|quit my job|burn(ed)? out"),
    ("advice",     r"looking for advice|any advice|thoughts on|what do you (all )?think|"
                   r"am i (behind|wrong)|should i\b"),
    ("spam",       r"we (will|'ll) get you \d+|\d+ (to )?\d+ new clients|dm me|inbox me|"
                   r"promo code|discount code|coupon|click (my )?link|check (out )?my (profile|site|link)"),
]

GIANT_COVER = {
    "bank_stmt": "银行直连 / Plaid / 各家银行 App",
    "reconcile": "Excel 函数 / 对账 SaaS",
    "invoice":   "QuickBooks / Xero / FreshBooks",
    "payroll":   "Gusto / ADP / QuickBooks Payroll",
    "tax":       "TurboTax / QuickBooks Tax / 报税软件",
    "csv_file":  "Excel Power Query / Python 脚本",
    "ledger":    "QuickBooks / Xero",
    "ap_ar":     "QuickBooks / Sage / NetSuite",
    "inventory": "QuickBooks / Shopify / 库存 SaaS",
    "expense":   "Expensify / Ramp / QuickBooks",
    "budget":    "YNAB / 表格自建",
    "report":    "Excel / Tableau / Looker",
    "formula":   "Excel 公式求助社区（免费替代品充足）",
    "format":    "Excel / Google Sheets（原生功能）",
    "email_doc": "Gmail / Outlook 插件生态",
    "pdf":       "Adobe / 扫描 + OCR 工具",
    "photo":     "Lightroom / Capture One",
    "video":     "Premiere / Resolve / CapCut",
    "subtitle":  "Subtitle Edit / Descript",
    "3dprint":   "PrusaSlicer / Cura",
    "keyboard":  "QMK / VIA（免费开源，但配置繁琐）",
    "fishing":    "垂钓 App（无强巨头）",
    "game":      "BoardGameGeek / Meetup",
    "model":     "涂装系统 / 手工工具",
    "diy":       "通用工具集（无强巨头）",
    "sysadmin":  "Zabbix / Grafana / 等",
    "api_data":  "Zapier / Make / n8n",
    "chase_docs": "QuickBooks 客户门户（功能有限）/ 邮件催收（无专用工具）",
    "scope_quote": "HoneyBook / Bidsketch 等（面向摄影/婚庆，非自由职业通用）",
    "prep_material": "Miro / Slite（协作白板，非演练材料专用）",
    # ---- v1.1.7 非财务领域 ----
    "seo_content": "Ahrefs / Semrush / Surfer（功能重叠严重，缝隙在细分行业长尾）",
    "ads_campaign": "Meta Ads / Google Ads / Klaviyo / Mailchimp",
    "ecom_ops": "Shopify App Store / 库存 ERP（缝隙在多平台同步与对账）",
    "social_content": "Descript / Opus Clip / CapCut / Canva",
    "crm_sales": "HubSpot / Salesforce / Pipedrive",
    "hr_ops": "BambooHR / Gusto / 排班 SaaS",
    "customer_support": "Zendesk / Intercom / Freshdesk",
    "realestate_ops": "MLS / AppFolio / Buildium",
    "health_admin": "SimplePractice / 保险理赔系统（合规敏感）",
    "legal_admin": "DocuSign / Ironclad（合同类已饱和，缝隙在行业模板）",
    "language_text": "DeepL / Trados（翻译已饱和，缝隙在垂类术语与校对）",
    "genealogy": "FamilySearch / Ancestry（免费档案库已很强）",
    "data_pipeline": "Zapier / n8n / Airbyte / Power Query",
    "devops_site": "Docker / Grafana / Uptime Kuma（开源替代已密集）",
    "design_asset": "Canva / Figma / Adobe（免费替代品极强）",
    "home_admin": "Zillow / 房产平台（交易端饱和，行政端有缝隙）",
    "other":     "—",
}

# 按工具/平台命名的社区 —— 提工具名在那里是基线，不是需求
TOOL_NAMED_SUBS = {"QuickBooks", "excel", "googlesheets", "spreadsheets", "Notion",
                   "PowerShell", "dataengineering"}

# JEV 判定（conf 0.72）
W_TOOL, W_INTENT = 0.4, 0.6


def load_posts():
    posts, seen = [], set()
    for f in sorted(os.listdir(RAW)):
        if not (f.startswith("raw") and f.endswith(".json")):
            continue
        for p in json.load(open(os.path.join(RAW, f), encoding="utf-8")):
            pid = p.get("post_id") or ""
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            posts.append(p)
    return posts


def pick(table, text, default="other"):
    """按表顺序取第一个命中的桶名。顺序即优先级（bank_stmt 在 invoice 前）。"""
    for name, pat in table:
        if re.search(pat, text):
            return name
    return default


def hit_pattern(table, name):
    """取回某个桶对应的正则 —— 用于「段内只保留真的命中该业务对象的帖」。"""
    for n, pat in table:
        if n == name:
            return pat
    return None


def flags(table, text):
    return {name for name, pat in table if re.search(pat, text)}


def main():
    posts = load_posts()
    rows = []
    for p in posts:
        title = p.get("title") or ""
        body = p.get("body") or ""
        txt = radar.norm(title + " . " + body)
        s = radar.extract(p)
        is_demand = bool(set(s["pain_hits"]) & DEMAND_CATS)
        nt = flags(NON_TOOL, txt)
        rows.append({
            "post_id": s["post_id"], "sub": s["sub"], "author": s["author"],
            "title": title, "created_utc": s["created_utc"],
            "fp": radar.fingerprint(title),
            "biz": pick(BIZ_LIST, txt),
            # v1.1.7：区分「标题命中该业务对象」与「正文顺带提到」。
            # 修正前日报里「预算与现金流」的原声是 "October Hardware Thread" ——
            # 因为桶判定扫全文，一篇讲硬件的帖正文提了一句 budget 就被归入预算桶。
            # 标题命中才是主题证据；只有标题命中才能当「用户原声」展示。
            "title_biz": pick(BIZ_LIST, radar.norm(title)),
            "_txt": txt,
            "is_demand": is_demand,
            "non_tool": sorted(nt),
            "workaround_score": s["workaround_score"], "kinds": s["_kinds"],
            "tm": flags(TOOL_MENTION, txt), "pi": flags(PAY_INTENT, txt),
            "pay_rules": s["pay_hits"], "freq": s["frequency"],
        })

    # ---- sub 基线（判断「提工具名」在该社区是不是常态）----
    sub_all, sub_pay = Counter(), Counter()
    for r in rows:
        sub_all[r["sub"]] += 1
        if r["tm"]:
            sub_pay[r["sub"]] += 1
    sub_base = {s: (sub_pay[s] / n if n else 0.0) for s, n in sub_all.items() if s}
    global_base = sum(1 for r in rows if r["tm"]) / len(rows)

    # ---- 细分 ----
    # ⚠️ v1.1.7 修正：候选帖要**真的命中该业务对象**才算进这个细分类。
    # 修正前：只要命中痛点词或工具名就进来（is_demand/tm/pi），
    # 导致「预算与现金流」段里混进 "October Hardware Thread"、"Changing Jobs" 这类
    # 完全无关的帖 —— 桶是按「第一个命中的模式」定的，一个帖只要沾一点预算/现金流词
    # 就被判进该桶，但它本身讲的是别的事。用户原声因此完全对不上。
    # 现在：桶名对应的正则必须在这个帖里再命中一次（other 桶例外，它本身就是兜底）。
    segs = defaultdict(list)
    for r in rows:
        if not (r["is_demand"] or r["tm"] or r["pi"]):
            continue
        pat = hit_pattern(BIZ_LIST, r["biz"])
        if r["biz"] != "other" and pat and not re.search(pat, r["_txt"], re.I):
            continue
        segs[r["biz"]].append(r)

    out = []
    for biz, items in segs.items():
        n = len(items)
        authors = {i["author"] for i in items if i["author"]}
        # JEV（灰区判定）：同作者只计 1 票，重复强度另算
        author_votes = Counter(i["author"] for i in items if i["author"])
        dup_intensity = (max(author_votes.values()) / len(author_votes)) if author_votes else 0.0
        # 只用「每个作者一票」的集合算比率，避免一人刷帖抬高比率
        one_per_author = {}
        for i in items:
            a = i["author"] or f"__pid_{i['post_id']}"
            one_per_author.setdefault(a, i)
        voted = list(one_per_author.values())
        nv = len(voted)

        def rate(key, src):
            return sum(1 for i in src if key in (i[src] if isinstance(src, str) else src)) / len(src)

        tm_rate = sum(1 for i in voted if i["tm"]) / nv
        pi_rate = sum(1 for i in voted if i["pi"]) / nv
        exp_base = sum(sub_base.get(i["sub"], global_base) for i in items) / n
        tool_named = sum(1 for i in items if i["tm"] and i["sub"] in TOOL_NAMED_SUBS)
        pay_items = [i for i in items if i["pi"]]
        by_sub = Counter(i["sub"] for i in pay_items)
        cross_sub = len([s for s, _ in by_sub.items() if s not in TOOL_NAMED_SUBS])
        non_tool_n = sum(1 for i in items if i["non_tool"])
        # JEV（choice conf 0.91）：拆两类，不简单加减分
        pay_score = 100 * (W_TOOL * tm_rate + W_INTENT * pi_rate)
        # v1.1.7：远程可做 + AI 可做 硬过滤。
        # ⚠️ 只把「业务对象 id + 中文名」喂给否决线判定，样本标题只作辅助证据
        # （实测直接喂 25 条标题会让 \bshift\b / \bcar\b 这类常见词整簇误杀）
        seg_text = f"{biz} {BIZ_CN.get(biz, '')}"
        ra = remote_ai.judge(seg_text, SENSITIVE_SUBS,
                             sample_titles=[i["title"] for i in items[:25]])
        if tm_rate > 0.5 and pi_rate < 0.15:
            budget_state = "预算已被巨头占住"
        elif pi_rate >= 0.30:
            budget_state = "预算在，且明确在找替代"
        elif pi_rate >= 0.15:
            budget_state = "预算在，不满但未表达替代意图"
        else:
            budget_state = "未验证付费意愿"
        out.append({
            "segment_id": biz, "biz_cn": BIZ_CN.get(biz, biz),
            "posts": n, "votes": nv, "unique_authors": len(authors),
            "dup_intensity": round(dup_intensity, 2),
            "first_fps": Counter(i["fp"] for i in items).most_common(3),
            "demand_posts": sum(1 for i in items if i["is_demand"]),
            "demand_ratio": round(sum(1 for i in items if i["is_demand"]) / n, 3),
            "tool_mention_rate": round(tm_rate, 3), "pay_intent_rate": round(pi_rate, 3),
            "pay_score": round(pay_score, 1), "budget_state": budget_state,
            "tool_excess": round(tm_rate - exp_base, 3),
            "alt_seek": sum(1 for i in items if "alt_seek" in i["pi"]),
            "will_pay": sum(1 for i in items if "will_pay" in i["pi"]),
            "too_expensive": sum(1 for i in items if "too_expensive" in i["pi"]),
            "workaround_rate": round(sum(1 for i in items if i["kinds"]) / n, 3),
            "non_tool_posts": non_tool_n,
            "non_tool_tags": Counter(t for i in items for t in i["non_tool"]).most_common(3),
            "subs": sorted({i["sub"] for i in items if i["sub"]}),
            "pay_anchor_subs": by_sub.most_common(5),
            "cross_sub_count": cross_sub,
            "giant_covered_by": GIANT_COVER.get(biz),
            "remote_ok": ra["remote_ok"], "ai_score": ra["ai_score"],
            "ai_level": ra["ai_level"], "remote_verdict": ra["verdict"],
            "compliance_tags": ra["compliance"], "remote_reasons": ra["reasons"],
            "remote_blockers": [b[0] for b in ra["blockers"]],
            # 原声只取「标题就命中该业务对象」的帖 —— 正文顺带提到不算主题证据
            "title_hit_posts": sum(1 for i in items if i["title_biz"] == biz),
            "sample_titles": [i["title"][:120] for i in items
                              if i["title_biz"] == biz and i["is_demand"]][:8],
        })
    # v1.1.7：远程闸门是准入 —— 不通过的排最后（不是直接丢，报告里要能看到并说明原因）
    out.sort(key=lambda x: (not x["remote_ok"], x["pay_score"], x["unique_authors"]), reverse=True)

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump({"generated_at": time.time(), "total_posts": len(rows),
               "sub_tool_baseline": {k: round(v, 4) for k, v in sorted(sub_base.items())},
               "global_tool_baseline": round(global_base, 4),
               "weights": {"tool_mention": W_TOOL, "pay_intent": W_INTENT},
               "segments": out}, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    L, W = [], None
    L = []
    W = L.append
    W("=" * 118)
    W(f"业务对象细分 v2（付费意愿排序）| {len(rows)} 帖 -> 候选帖 "
      f"{sum(1 for r in rows if r['is_demand'] or r['tm'] or r['pi'])} -> {len(out)} 个细分类")
    W(f"付费意愿分 = 工具名提及 {W_TOOL:.0%} + 主动意愿 {W_INTENT:.0%}（JEV conf 0.72；按作者去重计票）")
    W("=" * 118)
    W("")
    W(f"{'细分类':<26s}{'作者':>5s}{'票':>5s}{'痛点%':>7s}{'土办法%':>8s}"
      f"{'工具名%':>8s}{'主动意愿%':>10s}{'付费分':>8s}{'AI':>5s}  预算状态 / 远程")
    W("-" * 118)
    for s in out:
        if s["unique_authors"] < 4:
            continue
        flag = "" if s["remote_ok"] else "  ❌远程"
        W(f"{s['biz_cn']:<26s}{s['unique_authors']:>5d}{s['votes']:>5d}"
          f"{s['demand_ratio']:>7.1%}{s['workaround_rate']:>8.1%}{s['tool_mention_rate']:>8.1%}"
          f"{s['pay_intent_rate']:>10.1%}{s['pay_score']:>8.1f}{s['ai_score']:>5d}"
          f"  {s['budget_state']}{flag}")
    W("")
    W("付费意愿分的三个组成维度明细（谁在主动找替代）：")
    W(f"{'细分类':<26s}{'替代诉求':>9s}{'明说愿付':>9s}{'嫌贵':>7s}{'跨社区':>8s}{'非工具帖':>9s}  巨头覆盖方")
    W("-" * 118)
    for s in out:
        if s["unique_authors"] < 4:
            continue
        nt = ",".join(t for t, _ in s["non_tool_tags"]) or "-"
        W(f"{s['biz_cn']:<26s}{s['alt_seek']:>9d}{s['will_pay']:>9d}{s['too_expensive']:>7d}"
          f"{s['cross_sub_count']:>8d}{s['non_tool_posts']:>9d}  {s['giant_covered_by']}  [{nt}]")
    W("")
    W("=" * 118)
    open(OUT_TXT, "w", encoding="utf-8").write("\n".join(L))
    print("wrote", OUT_JSON)
    print("wrote", OUT_TXT)


if __name__ == "__main__":
    main()
