"""ORM 基类与共用表参数。

字典 §1「全库 utf8mb4」—— 中文知识库若不显式设 utf8mb4，
会出现中文乱码与排序/匹配异常，且只在真实数据进来后才暴露。
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 统一命名约定：让索引/外键/唯一约束的名字可预测。
# 不做这件事的话 MySQL 会自动生成名字，Alembic 后续 diff 时认不出同一个约束，
# 表现为「莫名其妙的 drop + create」。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 每张表都要带的表级参数。
# 不用「Base 上挂 __table_args__ 靠继承」的写法 —— 子类一旦为自己的
# 复合主键/索引定义 __table_args__，就会把继承来的字符集设置整个覆盖掉，
# 而且不报错。显式在每张表里带上，代价是重复一行，收益是不会静默丢设置。
TABLE_KWARGS: dict[str, str] = {
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
