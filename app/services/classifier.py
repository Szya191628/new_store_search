import re
from typing import Tuple

# 商业/临街实体特征词
COMMERCIAL_ADDRESS_KEYWORDS = (
    "底商", "商铺", "门面", "门市", "铺面", "摊位", "档口", "一层", "一楼", "1层", "1楼",
    "负一层", "负一楼", "B1", "b1", "商场", "市场", "广场", "商业街", "步行街", "美食街",
    "农贸", "菜市场", "综合体", "临街", "号附", "附", "铺",
)

STREET_KEYWORDS = ("路", "街", "道", "巷", "大道", "大街", "广场", "市场", "商场", "号")

# 明显非实体门面/虚拟/住宅特征
VIRTUAL_OR_CLUSTER_KEYWORDS = ("集群登记", "集群注册", "托管", "虚拟地址", "秘书公司", "集中办公区")
RESIDENTIAL_ROOM_RE = re.compile(r"(?:\d+|[一二三四五六七八九十]+)\s*(?:室|房|户)")
RESIDENTIAL_BUILDING_RE = re.compile(r"(?:小区|家园|花园|新村|公寓|苑|雅居|公馆|栋|单元|幢|座)")

# 非线下到店/本地生活行业黑名单关键词
NON_STORE_INDUSTRY_KEYWORDS = (
    "道路货物运输", "普通货运", "网络预约出租", "网约车", "出租汽车", "货运代理",
    "物流仓储", "供应链管理", "软件开发", "信息技术咨询", "信息系统集成",
    "人力资源", "劳务派遣", "企业管理咨询", "商务信息咨询", "财税咨询", "代理记账",
    "工程施工", "建筑劳务", "网络技术", "云计算", "大数据", "自媒体", "直播带货",
)

# 明确线下实体但需警惕纯网店的关键词
ECOMMERCE_ONLY_KEYWORDS = ("互联网销售", "网络销售", "淘宝", "拼多多", "抖音小店", "跨境电商", "电子商务")
OFFLINE_STORE_OVERRIDE_KEYWORDS = (
    "餐饮", "小吃", "饭店", "面馆", "火锅", "烧烤", "奶茶", "咖啡", "烘焙", "蛋糕",
    "美发", "理发", "美容", "美甲", "美睫", "足浴", "足疗", "按摩", "洗浴", "采耳",
    "五金", "建材", "汽修", "维修", "超市", "便利店", "百货", "水果", "生鲜", "药店",
    "干洗", "洗衣", "宠物", "台球", "棋牌", "健身", "瑜伽", "KTV", "茶楼", "茶馆",
    "堂食", "零售", "门店", "门市",
)

# 【到店消费型】关键词
EXPERIENCE_CONSUME_KEYWORDS = (
    "餐饮", "饭店", "餐馆", "酒楼", "小吃", "快餐", "面馆", "粉店", "饺子", "包子", "早餐",
    "火锅", "串串", "烧烤", "烤肉", "烤鱼", "龙虾", "川菜", "湘菜", "粤菜", "东北菜", "西餐", "日料",
    "奶茶", "茶饮", "咖啡", "饮品", "果汁", "甜品", "烘焙", "蛋糕", "面包", "糖水", "冰淇淋",
    "美容", "美发", "理发", "造型", "美甲", "美睫", "纹绣", "皮肤管理", "祛痘", "养发", "头疗",
    "洗浴", "足浴", "足疗", "按摩", "推拿", "SPA", "采耳", "汗蒸", "养生馆",
    "健身", "瑜伽", "普拉提", "拳击", "台球", "桌球", "棋牌", "茶室", "茶馆", "KTV", "ktv",
    "密室", "剧本杀", "网吧", "网咖", "电玩", "游乐", "轰趴", "酒吧", "清吧", "酒馆",
)

# 全国性大型连锁品牌黑名单（无需 BD 触达）
NATIONAL_CHAIN_BRANDS = (
    "瑞幸", "星巴克", "幸运咖", "库迪", "蜜雪冰城", "霸王茶姬", "喜茶", "奈雪", "古茗",
    "茶百道", "沪上阿姨", "书亦烧仙草", "一点点", "益禾堂", "CoCo都可",
    "麦当劳", "肯德基", "汉堡王", "华莱士", "塔斯汀", "德克士", "必胜客", "达美乐",
    "绝味鸭脖", "周黑鸭", "紫燕百味鸡", "正新鸡排", "袁记云饺", "熊大爷",
    "海底捞", "巴奴", "太二", "西贝", "萨莉亚", "兰湘子", "费大厨",
    "名创优品", "泡泡玛特", "罗森", "7-ELEVEN", "711", "全家便利", "美宜佳", "天福",
    "老百姓大药房", "益丰大药房", "大参林", "海王星辰",
)

# 执照前缀/后缀剥离规则
PREFIX_RE = re.compile(
    r"^(?:[\u4e00-\u9fa5]{2,8}(?:省|市|区|县|镇|街道|开发区|高新区|新区))+"
)
SUFFIX_WORDS = (
    "个体工商户", "有限公司", "有限责任公司", "工作室", "经营部", "服务部", "销售部",
    "商行", "商店", "百货店", "便利店", "餐饮店", "小吃店", "饭店", "茶饮店", "美容店",
    "美发店", "五金店", "建材店", "维修店", "汽车维修部", "店", "馆", "坊", "铺", "行",
)
STREET_EXTRACT_RE = re.compile(r"(?:省|市|区|县|镇|乡)?([\u4e00-\u9fa5]{2,8}(?:大道|大街|路|街|道|巷))")
ADMIN_PREFIX_RE = re.compile(r"^(?:[\u4e00-\u9fa5]{1,6}(?:省|市|区|县|镇|乡))+")


def is_physical_store_address(address: str) -> Tuple[bool, str]:
    addr = (address or "").strip()
    if not addr:
        return False, "地址为空"

    for kw in VIRTUAL_OR_CLUSTER_KEYWORDS:
        if kw in addr:
            return False, f"疑似虚拟/集群注册地址({kw})"

    has_commercial = any(kw in addr for kw in COMMERCIAL_ADDRESS_KEYWORDS)
    if has_commercial:
        return True, "含临街/商业体特征"

    # 如果是纯住宅房号（如 XX小区3栋2单元501室）且没有任何底商/一层特征，剔除
    if RESIDENTIAL_ROOM_RE.search(addr):
        if RESIDENTIAL_BUILDING_RE.search(addr) or "层" in addr or "楼" in addr:
            return False, "疑似纯住宅/写字楼房间(非临街底商)"

    if any(kw in addr for kw in STREET_KEYWORDS):
        return True, "含道路/门牌号特征"

    return False, "缺乏临街实体地址特征"


def is_non_store_industry(name: str, industry: str, scope: str) -> Tuple[bool, str]:
    text = f"{name} {industry} {scope}"
    has_offline_override = any(kw in text for kw in OFFLINE_STORE_OVERRIDE_KEYWORDS)

    for kw in NON_STORE_INDUSTRY_KEYWORDS:
        if kw in text and not has_offline_override:
            return True, f"非线下门店业态({kw})"

    if any(kw in text for kw in ECOMMERCE_ONLY_KEYWORDS) and not has_offline_override:
        return True, "疑似纯电商/网店"

    return False, ""


def classify_store_track(name: str, industry: str, scope: str) -> str:
    text = f"{name} {industry} {scope}"
    if any(kw in text for kw in EXPERIENCE_CONSUME_KEYWORDS):
        return "到店消费型"
    return "便民实体型"


def is_national_chain(name: str) -> bool:
    clean = (name or "").strip()
    return any(brand.lower() in clean.lower() for brand in NATIONAL_CHAIN_BRANDS)


def extract_core_trade_name(license_name: str) -> str:
    s = (license_name or "").strip()
    if not s:
        return ""
    s = re.sub(r"[（）()【】\[\]\s]+", "", s)
    s = PREFIX_RE.sub("", s)
    changed = True
    while changed and len(s) > 3:
        changed = False
        for suf in SUFFIX_WORDS:
            if s.endswith(suf) and len(s) - len(suf) >= 2:
                s = s[: -len(suf)]
                changed = True
                break
    return s or license_name.strip()


def extract_street_name(address: str) -> str:
    cleaned = ADMIN_PREFIX_RE.sub("", (address or "").strip())
    m = STREET_EXTRACT_RE.search(cleaned)
    return m.group(1) if m else ""
