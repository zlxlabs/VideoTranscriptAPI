import sys
from utils.llm import call_llm_api
from utils import load_config

def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/llm_test.py <txt文件路径>")
        return
    txt_path = sys.argv[1]
    with open(txt_path, 'r', encoding='utf-8') as f:
        transcript = f.read()
    config = load_config()
    api_key = config['llm']['api_key']
    base_url = config['llm']['base_url']
    calibrate_model = config['llm']['calibrate_model']
    summary_model = config['llm']['summary_model']
    calibrate_prompt = (
        "你将收到一段音频的转录文本。你的任务是对这段文本进行校对,提高其可读性,但不改变原意。 "
        "请按照以下指示进行校对: "
        "1. 适当分段,使文本结构更清晰。每个自然段落应该是一个完整的思想单元。 "
        "2. 修正明显的错别字和语法错误。 "
        "3. 调整标点符号的使用,确保其正确性和一致性。 "
        "4. 如有必要,可以轻微调整词序以提高可读性,但不要改变原意。 "
        "5. 保留原文中的口语化表达和说话者的语气特点。 "
        "6. 不要添加或删除任何实质性内容。 "
        "7. 不要解释或评论文本内容。 "
        "只返回校对后的文本,不要包含任何其他解释或评论。 "
        "以下是需要校对的转录文本: <transcript>  " + transcript + "  </transcript>"
    )
    summary_prompt = (
        "请以回车换行为分割，逐段地将正文内容，高度归纳提炼总结为凝炼的一句话，需涵盖主要内容，不能丢失关键信息和想表达的核心意思。用中文。然后将归纳总结的，用无序列表，挨个排列出来。\n"
        + transcript
    )
    print("【校对文本】\n")
    print(call_llm_api(calibrate_model, calibrate_prompt, api_key, base_url))
    print("\n【内容总结】\n")
    print(call_llm_api(summary_model, summary_prompt, api_key, base_url))

if __name__ == '__main__':
    main() 