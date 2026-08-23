"""数据库迁移脚本：初始化新表并迁移数据"""
import sys
sys.path.insert(0, 'D:/lixf/workbuddy')

from db.models_v2 import init_db, migrate_from_v1

print("=" * 60)
print("数据库迁移开始")
print("=" * 60)

print("\n[1/2] 创建新表...")
try:
    init_db()
    print("[OK] 新表创建成功")
except Exception as e:
    print(f"[ERROR] 创建表失败: {e}")
    sys.exit(1)

print("\n[2/2] 迁移现有数据...")
try:
    migrate_from_v1()
    print("[OK] 数据迁移成功")
except Exception as e:
    print(f"[ERROR] 数据迁移失败: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("数据库迁移完成！")
print("=" * 60)
