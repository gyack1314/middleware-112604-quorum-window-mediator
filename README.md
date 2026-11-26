# 合成演练指令的成员重组与法定人数确认

成员和窗口资源由简短合成标识表示。配置的 required 条款为显式必须参与者。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **roster**　scope 的 revision，members、minimum、required 与 prev。prev 非空时过渡阶段同时检查前后两套条件。
- **proposal**　scope 的 id、rev、roster 和 text。
- **vote**　member 对 id/rev 给 yes、no 或 withdraw，引用 roster。
- **grant**　要求判定是否能下发。
- **started**　模拟端接受指定 id/rev 的已授动作。
- **settled**　模拟端回报 state，done 或 stopped，附相同 id/rev。
- **restart**　模拟服务重启。
- **inspect**　查看 scope 或 id。

## 示例

- joint-membership-transition.jsonl
- withdraw-after-real-start.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。
