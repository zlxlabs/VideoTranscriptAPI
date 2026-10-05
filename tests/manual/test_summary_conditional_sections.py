"""
Test conditional sections (Logic Analysis / Framework) in summary prompt

This test verifies that:
1. Product review content should NOT generate "Framework" section
2. Interview content with methodology sharing SHOULD generate "Framework" section
"""
import os
import sys

try:
    import commentjson as json
except ImportError:
    import json

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, project_root)

from src.video_transcript_api.llm import LLMCoordinator
from src.video_transcript_api.utils.logging import setup_logger

logger = setup_logger(__name__)

# Test case 1: Product review (should NOT generate Framework section)
PRODUCT_REVIEW_TEXT = """
市面上有很多网盘，每家都要花钱购买VIP，那到底哪些好用？哪些对免费用户更良心？之前都流行从行到拉的排行榜，而果核准备做一个长期系列，深度横评一下国内网盘。不主观推荐，不预设立场，所有数据来源于官方公开信息及实测，用各项数据来拆解各大网盘的真实素质，帮你找到适合的网盘。

先看一下10家网盘的免费空间容量对比，123云盘领先，提供免费用户2TB空间，其次是百度网盘1TB，阿里云盘100GB。天翼云盘对新用户提供60GB空间，夸克网盘10GB。

再来看付费成本。我们用元/TB/月这个统一指标来对比。123云盘最便宜，大约0.9元/TB/月。百度网盘约2.5元/TB/月。阿里云盘约3元/TB/月。

需要注意的是，115网盘和夸克的8-10年长期方案成本极低，算下来只要6分钱/TB/月，但存在权益调整风险。

各网盘还有一些特色功能。天翼云盘支持家庭共享，最多可以绑定5个家庭成员。蓝奏云提供无限空间，但限制单文件100MB。百度网盘的生态最完善，支持各种第三方应用。

总结一下，如果追求性价比，123云盘是最好的选择。如果需要家庭共享，推荐天翼云盘。如果存小文件为主，蓝奏云无限空间很香。
"""

PRODUCT_REVIEW_META = {
    'video_title': '10款网盘性价比深度横评',
    'author': '果核剥壳',
    'description': '对比10款国内主流网盘的免费空间、付费成本、特色功能，帮你找到最适合的网盘。'
}

# Test case 2: Interview with methodology (SHOULD generate Framework section)
INTERVIEW_WITH_METHOD_TEXT = """
主持人：今天我们请到了张总，他创业10年，把公司从零做到了上市。张总，能分享一下你的创业方法论吗？

张总：当然可以。我总结了一套"三步验证法"，这是我创业过程中反复验证过的方法。

第一步叫"需求验证"。在投入任何资源之前，先用最小成本验证市场需求。我们当时就是先做了一个简单的落地页，看有多少人愿意留下联系方式。如果转化率低于5%，说明这个需求可能是伪需求，或者你的表达方式有问题。这一步花的钱应该控制在1万以内。

第二步是"模式验证"。需求验证通过后，要验证商业模式能不能跑通。我建议做一个最小可行产品，用真实用户来测试。重点关注几个指标：用户留存率、付费转化率、用户获取成本。如果单位经济模型算不过来，就要调整定价或者成本结构。

第三步是"规模验证"。前两步都通过了，才考虑规模化。这时候要验证的是，你的模式能不能复制，成本结构能不能优化。很多公司死在这一步，因为规模化之后边际成本没有降低，反而上升了。

主持人：这个三步验证法很实用。那在执行过程中有什么需要注意的吗？

张总：最重要的是控制每一步的成本。我见过太多创业者，需求还没验证清楚就砸钱做产品。记住，验证的目的是降低风险，不是证明自己是对的。

另外还有一个"721法则"：70%的资源用于已验证的业务，20%用于有潜力的新方向，10%用于探索性尝试。这样既能保证主业稳定，又能持续创新。

主持人：张总提到的这些方法都很实用。那您觉得创业最重要的是什么？

张总：我认为是"认知升级"。创业本质上是认知变现。你对市场的理解、对用户的理解、对商业模式的理解，决定了你能走多远。所以我每天都会花2小时学习，看书、听播客、和同行交流。持续学习是创业者最重要的习惯。
"""

INTERVIEW_WITH_METHOD_META = {
    'video_title': '创业10年，我总结了这套方法论',
    'author': '创业访谈录',
    'description': '专访上市公司创始人张总，分享他的创业三步验证法。'
}


def load_config():
    """Load configuration from config file"""
    config_path = os.path.join(project_root, 'config', 'config.jsonc')

    if not os.path.exists(config_path):
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def check_section_exists(summary: str, section_name: str) -> bool:
    """Check if a section exists in the summary"""
    import re
    if not summary:
        return False
    pattern = rf'#{2,4}\s*\d*\.?\s*{section_name}'
    return bool(re.search(pattern, summary))


def run_test(coordinator, test_text: str, meta: dict, test_name: str) -> dict:
    """Run a single test case"""
    logger.info(f"\n{'='*60}")
    logger.info(f"Running test: {test_name}")
    logger.info(f"{'='*60}")

    result = coordinator.process(
        content=test_text,
        title=meta['video_title'],
        author=meta['author'],
        description=meta['description'],
        platform='test',
        media_id=f"test_{test_name}",
    )

    summary = result.get('summary_text', '')

    has_logic = check_section_exists(summary, '逻辑分析')
    has_framework = check_section_exists(summary, '框架')

    return {
        'test_name': test_name,
        'summary': summary,
        'has_logic_section': has_logic,
        'has_framework_section': has_framework
    }


def main():
    """Main test function"""
    logger.info("Starting conditional sections test")

    config = load_config()

    output_dir = os.path.join(project_root, 'tests', 'llm', 'output')
    os.makedirs(output_dir, exist_ok=True)
    cache_dir = os.path.join(output_dir, 'cache')
    os.makedirs(cache_dir, exist_ok=True)

    coordinator = LLMCoordinator(config_dict=config, cache_dir=cache_dir)

    # Run test 1: Product review
    result1 = run_test(
        coordinator,
        PRODUCT_REVIEW_TEXT,
        PRODUCT_REVIEW_META,
        'product_review'
    )

    # Run test 2: Interview with methodology
    result2 = run_test(
        coordinator,
        INTERVIEW_WITH_METHOD_TEXT,
        INTERVIEW_WITH_METHOD_META,
        'interview_methodology'
    )

    # Save results
    with open(os.path.join(output_dir, 'summary_product_review.txt'), 'w', encoding='utf-8') as f:
        f.write(result1['summary'])

    with open(os.path.join(output_dir, 'summary_interview.txt'), 'w', encoding='utf-8') as f:
        f.write(result2['summary'])

    # Print results
    logger.info(f"\n{'='*60}")
    logger.info("TEST RESULTS")
    logger.info(f"{'='*60}")

    logger.info(f"\n[Test 1] Product Review - {PRODUCT_REVIEW_META['video_title']}")
    logger.info(f"  Logic Analysis section: {'FOUND' if result1['has_logic_section'] else 'NOT FOUND'}")
    logger.info(f"  Framework section: {'FOUND' if result1['has_framework_section'] else 'NOT FOUND'}")
    if result1['has_framework_section']:
        logger.warning("  WARNING: Framework section should NOT exist for product reviews!")
    else:
        logger.info("  OK: Correctly skipped Framework section")

    logger.info(f"\n[Test 2] Interview with Methodology - {INTERVIEW_WITH_METHOD_META['video_title']}")
    logger.info(f"  Logic Analysis section: {'FOUND' if result2['has_logic_section'] else 'NOT FOUND'}")
    logger.info(f"  Framework section: {'FOUND' if result2['has_framework_section'] else 'NOT FOUND'}")
    if result2['has_framework_section']:
        logger.info("  OK: Correctly generated Framework section for methodology content")
    else:
        logger.warning("  WARNING: Framework section SHOULD exist for methodology sharing!")

    logger.info(f"\n{'='*60}")
    logger.info(f"Output saved to: {output_dir}")
    logger.info(f"{'='*60}")


if __name__ == '__main__':
    main()
