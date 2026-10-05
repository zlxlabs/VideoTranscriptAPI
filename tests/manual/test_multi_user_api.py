"""
多用户API手动测试脚本

测试新的多用户鉴权和审计功能。
注意：需要先启动API服务器才能运行此测试。
"""

import requests
import json
import time
import sys
import os

# API服务器配置
API_BASE_URL = "http://localhost:8000"

# 测试用户配置
TEST_USERS = {
    "user1": {
        "token": "sk-test001-abcdefghij",
        "name": "测试用户1"
    },
    "user2": {
        "token": "sk-test002-klmnopqrst", 
        "name": "测试用户2"
    },
    "invalid": {
        "token": "invalid-token",
        "name": "无效用户"
    }
}


def make_request(method, endpoint, token=None, data=None, params=None):
    """
    发送HTTP请求的辅助函数
    
    Args:
        method: HTTP方法 (GET, POST, etc.)
        endpoint: API端点
        token: 认证令牌
        data: 请求体数据
        params: URL参数
        
    Returns:
        tuple: (是否成功, 响应对象)
    """
    url = f"{API_BASE_URL}{endpoint}"
    headers = {
        "Content-Type": "application/json"
    }
    
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, params=params)
        elif method.upper() == "POST":
            response = requests.post(url, headers=headers, json=data)
        else:
            response = requests.request(method, url, headers=headers, json=data, params=params)
        
        return True, response
    except Exception as e:
        print(f"请求失败: {str(e)}")
        return False, None


def test_authentication():
    """测试用户认证功能"""
    print("=" * 50)
    print("测试用户认证功能")
    print("=" * 50)
    
    # 测试有效用户认证
    print("\n1. 测试有效用户认证")
    success, response = make_request("GET", "/api/users/profile", TEST_USERS["user1"]["token"])
    if success and response.status_code == 200:
        print("✅ 有效用户认证成功")
        data = response.json()
        print(f"   用户信息: {data.get('data', {}).get('user_info', {}).get('name', 'Unknown')}")
    else:
        print(f"❌ 有效用户认证失败: {response.status_code if response else 'Network Error'}")
    
    # 测试无效用户认证
    print("\n2. 测试无效用户认证")
    success, response = make_request("GET", "/api/users/profile", TEST_USERS["invalid"]["token"])
    if success and response.status_code == 401:
        print("✅ 无效用户正确被拒绝")
    else:
        print(f"❌ 无效用户认证处理异常: {response.status_code if response else 'Network Error'}")
    
    # 测试缺少令牌
    print("\n3. 测试缺少认证令牌")
    success, response = make_request("GET", "/api/users/profile")
    if success and response.status_code == 401:
        print("✅ 缺少令牌正确被拒绝")
    else:
        print(f"❌ 缺少令牌处理异常: {response.status_code if response else 'Network Error'}")


def test_audit_logging():
    """测试审计日志功能"""
    print("\n" + "=" * 50)
    print("测试审计日志功能")
    print("=" * 50)
    
    # 先进行几次API调用生成日志
    print("\n1. 生成审计日志数据")
    for i, (user_key, user_info) in enumerate(TEST_USERS.items()):
        if user_key == "invalid":
            continue
        
        print(f"   用户 {user_info['name']} 调用API...")
        success, response = make_request("GET", "/api/users/profile", user_info["token"])
        if success:
            print(f"   ✅ 用户 {user_info['name']} API调用成功")
        else:
            print(f"   ❌ 用户 {user_info['name']} API调用失败")
        
        time.sleep(0.5)  # 短暂延迟
    
    # 测试获取统计信息
    print("\n2. 测试获取用户统计信息")
    success, response = make_request("GET", "/api/audit/stats", TEST_USERS["user1"]["token"], params={"days": 1})
    if success and response.status_code == 200:
        print("✅ 获取统计信息成功")
        data = response.json()
        stats = data.get("data", {}).get("user_stats", {})
        print(f"   总调用次数: {stats.get('total_calls', 0)}")
        print(f"   活跃天数: {stats.get('active_days', 0)}")
        print(f"   平均处理时间: {stats.get('avg_processing_time_ms', 0)}ms")
    else:
        print(f"❌ 获取统计信息失败: {response.status_code if response else 'Network Error'}")
    
    # 测试获取调用记录
    print("\n3. 测试获取调用记录")
    success, response = make_request("GET", "/api/audit/calls", TEST_USERS["user1"]["token"], params={"limit": 10})
    if success and response.status_code == 200:
        print("✅ 获取调用记录成功")
        data = response.json()
        calls = data.get("data", {}).get("calls", [])
        print(f"   最近调用记录数: {len(calls)}")
        if calls:
            latest_call = calls[0]
            print(f"   最新调用: {latest_call.get('endpoint', 'Unknown')} at {latest_call.get('request_time', 'Unknown')}")
    else:
        print(f"❌ 获取调用记录失败: {response.status_code if response else 'Network Error'}")


def test_user_profile():
    """测试用户配置功能"""
    print("\n" + "=" * 50)
    print("测试用户配置功能")
    print("=" * 50)
    
    for user_key, user_info in TEST_USERS.items():
        if user_key == "invalid":
            continue
        
        print(f"\n测试用户: {user_info['name']}")
        success, response = make_request("GET", "/api/users/profile", user_info["token"])
        
        if success and response.status_code == 200:
            print(f"✅ 获取 {user_info['name']} 配置成功")
            data = response.json()
            user_profile = data.get("data", {}).get("user_info", {})
            
            print(f"   用户ID: {user_profile.get('user_id', 'Unknown')}")
            print(f"   用户名: {user_profile.get('name', 'Unknown')}")
            print(f"   API密钥: {user_profile.get('api_key', 'Unknown')}")
            print(f"   启用状态: {user_profile.get('enabled', False)}")
            print(f"   企微Webhook: {user_profile.get('wechat_webhook', 'Not configured')[:50]}...")
        else:
            print(f"❌ 获取 {user_info['name']} 配置失败: {response.status_code if response else 'Network Error'}")


def test_cross_user_access():
    """测试跨用户访问控制"""
    print("\n" + "=" * 50)
    print("测试跨用户访问控制")
    print("=" * 50)
    
    # 用户1获取统计信息（应该只能看到自己的）
    print("\n1. 用户1获取统计信息")
    success, response = make_request("GET", "/api/audit/stats", TEST_USERS["user1"]["token"])
    if success and response.status_code == 200:
        data = response.json()
        user_stats = data.get("data", {}).get("user_stats", {})
        user_id = user_stats.get("user_id")
        print(f"✅ 用户1只能看到自己的统计 (user_id: {user_id})")
        
        # 验证这确实是用户1的数据
        expected_user_id = "test_user_001"  # 基于我们在测试配置中的设定
        if user_id == expected_user_id:
            print("✅ 访问控制正确：用户只能看到自己的数据")
        else:
            print(f"⚠️  用户ID不匹配，期望: {expected_user_id}, 实际: {user_id}")
    else:
        print(f"❌ 获取统计信息失败: {response.status_code if response else 'Network Error'}")


def check_server_availability():
    """检查服务器是否可用"""
    print("检查API服务器可用性...")
    try:
        response = requests.get(f"{API_BASE_URL}/", timeout=5)
        return True
    except:
        return False


def main():
    """主测试函数"""
    print("多用户API功能测试")
    print("=" * 60)
    
    # 检查服务器是否运行
    if not check_server_availability():
        print("❌ API服务器不可用！")
        print("请确保API服务器在 http://localhost:8000 上运行")
        print("启动命令: python main.py --start")
        sys.exit(1)
    
    print("✅ API服务器可用")
    
    # 检查是否有用户配置文件
    users_config_path = "config/users.json"
    if not os.path.exists(users_config_path):
        print(f"\n⚠️  警告: 用户配置文件 {users_config_path} 不存在")
        print("系统将使用单token回退模式")
        print("如需测试多用户功能，请创建用户配置文件")
    
    # 运行测试
    try:
        test_authentication()
        test_user_profile()
        test_audit_logging()
        test_cross_user_access()
        
        print("\n" + "=" * 60)
        print("✅ 多用户API功能测试完成")
        print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n\n⚠️  测试被用户中断")
    except Exception as e:
        print(f"\n\n❌ 测试过程中发生异常: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()