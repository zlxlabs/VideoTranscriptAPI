#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
缓存数据迁移脚本

合并和迁移分散的缓存数据库文件和缓存文件到统一的 data/cache 目录
"""

import os
import sys
import sqlite3
import shutil
import json
from pathlib import Path

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from video_transcript_api.utils.logger import setup_logger

def get_project_root():
    """获取项目根目录"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_config():
    """加载配置文件"""
    config_path = os.path.join(get_project_root(), "config", "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def merge_databases(source_db_path, target_db_path, logger):
    """
    合并两个SQLite数据库
    
    Args:
        source_db_path: 源数据库路径
        target_db_path: 目标数据库路径
        logger: 日志记录器
    """
    if not os.path.exists(source_db_path):
        logger.info(f"源数据库不存在: {source_db_path}")
        return
    
    # 创建目标目录
    os.makedirs(os.path.dirname(target_db_path), exist_ok=True)
    
    # 如果目标数据库不存在，直接复制
    if not os.path.exists(target_db_path):
        shutil.copy2(source_db_path, target_db_path)
        logger.info(f"复制数据库: {source_db_path} -> {target_db_path}")
        return
    
    # 合并数据库
    try:
        # 连接目标数据库
        target_conn = sqlite3.connect(target_db_path)
        target_cursor = target_conn.cursor()
        
        # 连接源数据库
        source_conn = sqlite3.connect(source_db_path)
        source_cursor = source_conn.cursor()
        
        # 获取源数据库的表结构
        source_cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = source_cursor.fetchall()
        
        for table_name, in tables:
            if table_name == 'sqlite_sequence':
                continue
                
            logger.info(f"处理表: {table_name}")
            
            # 获取表结构
            source_cursor.execute(f"PRAGMA table_info({table_name});")
            columns_info = source_cursor.fetchall()
            
            # 检查目标数据库是否有该表
            target_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table_name,))
            if not target_cursor.fetchone():
                # 创建表
                source_cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table_name}';")
                create_sql = source_cursor.fetchone()[0]
                target_cursor.execute(create_sql)
                logger.info(f"创建表: {table_name}")
            
            # 获取主键列
            primary_keys = [col[1] for col in columns_info if col[5] == 1]
            
            # 获取源数据
            source_cursor.execute(f"SELECT * FROM {table_name};")
            rows = source_cursor.fetchall()
            
            # 获取列名
            column_names = [col[1] for col in columns_info]
            
            merged_count = 0
            skipped_count = 0
            
            for row in rows:
                try:
                    # 检查是否已存在 (基于主键)
                    if primary_keys:
                        where_clause = " AND ".join([f"{pk} = ?" for pk in primary_keys])
                        pk_values = [row[column_names.index(pk)] for pk in primary_keys]
                        
                        target_cursor.execute(f"SELECT 1 FROM {table_name} WHERE {where_clause};", pk_values)
                        if target_cursor.fetchone():
                            skipped_count += 1
                            continue
                    
                    # 插入数据
                    placeholders = ",".join(["?" for _ in row])
                    target_cursor.execute(f"INSERT INTO {table_name} VALUES ({placeholders});", row)
                    merged_count += 1
                    
                except sqlite3.IntegrityError as e:
                    skipped_count += 1
                    logger.debug(f"跳过重复记录: {e}")
            
            logger.info(f"表 {table_name}: 合并 {merged_count} 条记录, 跳过 {skipped_count} 条重复记录")
        
        # 提交并关闭连接
        target_conn.commit()
        target_conn.close()
        source_conn.close()
        
        logger.info(f"数据库合并完成: {source_db_path} -> {target_db_path}")
        
    except Exception as e:
        logger.error(f"数据库合并失败: {e}")
        raise

def move_cache_files(source_dir, target_dir, logger):
    """
    移动缓存文件，避免重复
    
    Args:
        source_dir: 源目录
        target_dir: 目标目录
        logger: 日志记录器
    """
    if not os.path.exists(source_dir):
        logger.info(f"源目录不存在: {source_dir}")
        return
    
    os.makedirs(target_dir, exist_ok=True)
    
    moved_count = 0
    skipped_count = 0
    
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            source_file = os.path.join(root, file)
            
            # 计算相对路径
            rel_path = os.path.relpath(source_file, source_dir)
            target_file = os.path.join(target_dir, rel_path)
            
            # 创建目标目录
            os.makedirs(os.path.dirname(target_file), exist_ok=True)
            
            # 检查文件是否已存在
            if os.path.exists(target_file):
                # 比较文件大小和修改时间
                source_stat = os.stat(source_file)
                target_stat = os.stat(target_file)
                
                if source_stat.st_size == target_stat.st_size and source_stat.st_mtime <= target_stat.st_mtime:
                    skipped_count += 1
                    continue
            
            # 移动文件
            try:
                shutil.move(source_file, target_file)
                moved_count += 1
            except (PermissionError, OSError) as e:
                logger.warning(f"文件被占用，请手动删除: {source_file}")
                skipped_count += 1
    
    logger.info(f"文件移动完成: 移动 {moved_count} 个文件, 跳过 {skipped_count} 个重复文件")

def update_config_paths(logger):
    """更新配置文件中的路径"""
    project_root = get_project_root()
    config_path = os.path.join(project_root, "config", "config.json")
    
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    
    # 更新路径配置
    updates = {
        "storage.cache_dir": "./data/cache",
        "storage.temp_dir": "./data/temp", 
        "log.file": "./data/logs/app.log"
    }
    
    updated = False
    for key, new_value in updates.items():
        keys = key.split('.')
        current = config
        
        # 导航到目标位置
        for k in keys[:-1]:
            if k not in current:
                current[k] = {}
            current = current[k]
        
        # 更新值
        if keys[-1] not in current or current[keys[-1]] != new_value:
            old_value = current.get(keys[-1], "未设置")
            current[keys[-1]] = new_value
            logger.info(f"更新配置 {key}: {old_value} -> {new_value}")
            updated = True
    
    if updated:
        # 备份原配置文件
        backup_path = config_path + ".backup"
        shutil.copy2(config_path, backup_path)
        logger.info(f"配置文件已备份到: {backup_path}")
        
        # 写入新配置
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info("配置文件已更新")
    else:
        logger.info("配置文件无需更新")

def main():
    """主函数"""
    project_root = get_project_root()
    
    # 设置日志
    logger = setup_logger("cache_migration")
    logger.info("开始缓存数据迁移...")
    
    # 定义路径
    target_cache_dir = os.path.join(project_root, "data", "cache")
    target_db_path = os.path.join(target_cache_dir, "cache.db")
    
    # 需要迁移的数据库文件
    db_sources = [
        os.path.join(project_root, "cache_dir", "cache.db"),
        os.path.join(project_root, "data", "cache", "cache.db"),
        os.path.join(project_root, "data", "cache", "cache_dir", "cache.db"),
        os.path.join(project_root, "data", "test_cache.db")
    ]
    
    # 创建临时的合并数据库
    temp_db_path = os.path.join(target_cache_dir, "cache_temp.db")
    
    # 合并所有数据库
    for source_db in db_sources:
        if os.path.exists(source_db) and source_db != target_db_path:
            merge_databases(source_db, temp_db_path, logger)
    
    # 替换目标数据库
    if os.path.exists(temp_db_path):
        if os.path.exists(target_db_path):
            backup_db_path = target_db_path + ".backup"
            shutil.move(target_db_path, backup_db_path)
            logger.info(f"原数据库已备份到: {backup_db_path}")
        
        shutil.move(temp_db_path, target_db_path)
        logger.info(f"数据库迁移完成: {target_db_path}")
    
    # 迁移缓存文件
    cache_sources = [
        os.path.join(project_root, "cache_dir"),
        os.path.join(project_root, "data", "cache", "cache_dir")
    ]
    
    for source_cache in cache_sources:
        if os.path.exists(source_cache):
            # 排除数据库文件
            for root, dirs, files in os.walk(source_cache):
                for file in files:
                    if file.endswith('.db'):
                        db_file = os.path.join(root, file)
                        try:
                            os.remove(db_file)
                            logger.info(f"删除已迁移的数据库文件: {db_file}")
                        except (PermissionError, OSError) as e:
                            logger.warning(f"无法删除数据库文件 {db_file}: {e}，将跳过")
            
            # 移动其他文件
            move_cache_files(source_cache, target_cache_dir, logger)
    
    # 更新配置文件
    update_config_paths(logger)
    
    # 清理空目录
    cleanup_dirs = [
        os.path.join(project_root, "cache_dir"),
        os.path.join(project_root, "data", "cache", "cache_dir")
    ]
    
    for cleanup_dir in cleanup_dirs:
        if os.path.exists(cleanup_dir):
            try:
                shutil.rmtree(cleanup_dir)
                logger.info(f"删除空目录: {cleanup_dir}")
            except OSError as e:
                logger.warning(f"无法删除目录 {cleanup_dir}: {e}")
    
    logger.info("缓存数据迁移完成！")
    logger.info(f"统一的缓存目录: {target_cache_dir}")
    logger.info(f"主数据库文件: {target_db_path}")

if __name__ == "__main__":
    main()