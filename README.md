# 药用植物采收窗口

该项目保存地块、物候、投入品、抽样检测、天气和作业决定。观察同时记录实际发生时间与录入时间，便于解释迟到资料对在途作业的影响。

`farm/contracts.py` 定义观察和决策版本，`fixtures/harvest_window.json` 包含补录施用与突发降雨。项目使用 Python 3.11，可执行 `python -m compileall farm` 检查契约。
