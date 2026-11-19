"""P0/P1/P2 第三阶段：配额记账挂到调用点 + app.py 接线。"""
import sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()


def sub(old, new, why=""):
    global src
    assert old in src, f"未匹配: {why or old[:60]}"
    src = src.replace(old, new, 1)


# _call_one 记一次用量
sub(
    '''    def _call_one(self, be: "Backend", data_url: str, prompt: str, image_bytes: bytes) -> dict:
        """对单个后端发一次请求，返回结构化结果（不抛异常）。"""''',
    '''    def _call_one(self, be: "Backend", data_url: str, prompt: str, image_bytes: bytes) -> dict:
        """对单个后端发一次请求，返回结构化结果（不抛异常）。"""
        if self.quota is not None:
            self.quota.record(self.account_of(be))''',
    "_call_one 记账",
)

open(path, "w", encoding="utf-8").write(src)
print("resilience.py: _call_one 记账完成")
