"""Golden dataset for regression testing: known stock + known indicator values."""

# 10 classic A-share stocks covering main sectors
GOLDEN_STOCKS = [
    ("000001.SZ", "000001", "平安银行", "SZSE"),
    ("600036.SH", "600036", "招商银行", "SSE"),
    ("000858.SZ", "000858", "五粮液", "SZSE"),
    ("600519.SH", "600519", "贵州茅台", "SSE"),
    ("300750.SZ", "300750", "宁德时代", "SZSE"),
    ("688981.SH", "688981", "中芯国际", "SSE"),
    ("000333.SZ", "000333", "美的集团", "SZSE"),
    ("600276.SH", "600276", "恒瑞医药", "SSE"),
    ("002415.SZ", "002415", "海康威视", "SZSE"),
    ("300059.SZ", "300059", "东方财富", "SZSE"),
]

# Extreme market dates for edge case testing
EXTREME_DATES = [
    "20150708",  # 股灾底
    "20200323",  # COVID low
    "20240205",  # Recent low
]
