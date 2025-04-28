不要修改本文件，关于项目的说明请在  Project_README.md 里完成。

# 核心功能
请用 python 帮我写一个项目，完成以下功能：
- 对外暴露一个 api 端口，接收一个视频 url 作为参数，返回视频的转录文本。
- 获取转录文本的两种文案
    - 调用下载器下载 url 里的视频或者音频到本地：比如抖音，bilibili
    - 调用字幕下载 API直接下载平台生成的字幕：比如 youtube
- 转录成功则删除过程中下载的音视频文件，保留字幕文件即可。
- 支持一定的并发数量，所有需要改动的配置文件都应该写在 config.json 文件里。

# 分步骤详解
## 项目 API 接口
- post 请求，需要进行鉴权，鉴权 token 配置在 config 文件里。
- 请求参数 {"url":""}
- 返回参数中 转录文本对应的 key 为 "transcript",视频标题对应的 key 为 "video_title"，视频作者对应的 key 为 "author",其他的错误码、错误信息的 key 符合常见标准即可。

## 视频下载方法
- 平台传过来的链接通常是短链接，所以需要解析短链接以获取原始的长链接。
- 通过长链接 url 获取视频下载地址： https://api.tikhub.io ，支持 youtube 字幕和视频地址、抖音、小红书、bilibili 视频
- 有 mp3 的情况下优先下载 mp3 文件到本地，否则应该下载码率最低的 mp4 文件已节省存储空间。
- 大部分的 api 响应 json 很长，我在下面文档里只以 json path 说明需要提取的参数，完整的响应结果我放在 sample_files 里了。

### 抖音
短链接：https://v.douyin.com/rzK48SiNhJE/ 
解析长链接：https://www.douyin.com/video/74770599505779786363
获取视频下载地址请求：
```
curl -X 'GET' \
  'https://api.tikhub.io/api/v1/douyin/web/fetch_one_video?aweme_id=7477059950577978636' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer tokenxxx'
```
响应 json 很长，仅提供必要 key 的 json path：
```
data.aweme_detail.author.nickname：作者名
data.aweme_detail.item_title:视频名称
data.aweme_detail.video.bit_rate_audio[0].audio_meta.url_list.main_url: mp3 的音频下载地址
```

### bilibili
短链接：https://b23.tv/CpOgR16
长链接：https://www.bilibili.com/video/BV1JBLozmEFi?-Arouter=story
请求：
```
curl -X 'GET' \
  'https://api.tikhub.io/api/v1/bilibili/web/fetch_one_video?bv_id=BV1Pho9YrEre' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer tokenxxx'
```
响应 json 很长，仅提供必要 key 的 json path
```
data.data.title: 视频标题
data.data.owner.name :视频作者
data.data.cid
data.data.bvid
```
然后使用 bvid 和 cid 请求获取视频流地址
```
curl -X 'GET' \
  'https://api.tikhub.io/api/v1/bilibili/web/fetch_video_playurl?bv_id=BV1Pho9YrEre&cid=29108798989' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer tokenxxx'
```  
其响应结果中需提取的信息：
```
data.data.dash.audio[0].baseUrl : 音频文件地址
```


### 小红书视频
短链接：http://xhslink.com/a/sTDXmexS0aebb
长链接：https://www.xiaohongshu.com/explore/67e7beb7000000000f03adfe
请求
```
curl -X 'GET' \
  'https://api.tikhub.io/api/v1/xiaohongshu/web/get_note_info?note_id=67e7beb7000000000f03adfe' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer tokenxxx'
```
响应 json 很长，仅提供必要 key 的 json path

```
data.data.data[0].note_list[0].video.url ：视频下载地址
data.data.data[0].user.name ：视频作者
data.data.data[0].note_list[0].title
```

### Youtube 
短链接：https://youtu.be/AMCUqgu_cTM?si=Lx1Pq_HE8rhkA5HX
长链接：https://www.youtube.com/watch?v=AMCUqgu_cTM
获取视频下载地址 api 请求
```
curl -X 'GET' \
  'https://api.tikhub.io/api/v1/youtube/web/get_video_info?video_id=AMCUqgu_cTM' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer tokenxxx'
```
响应结果
```
data.audios.items[0].url   : 音频下载地址
data.channel.name :视频作者
data.title : 视频标题

data.subtitles 里存放着平台的字幕信息。有的视频有字幕，就可以直接下载字幕，合并后当做 transcript；如果没有再下载原始音频文件，进行转录。
其文件结构如下
{
      "status": true,
      "errorId": "Success",
      "expiration": 1745832245,
      "items": [
        {
          "url": "https://www.youtube.com/api/timedtext?v=AMCUqgu_cTM&ei=xeYOaN_bIvSKkucPp67p2A0&caps=asr&opi=112496729&exp=xpo&xoaf=7&hl=en&ip=0.0.0.0&ipbits=0&expire=1745832245&sparams=ip,ipbits,expire,v,ei,caps,opi,exp,xoaf&signature=1EF9185C6FCC876C2D3DA5D3B24C8E803D7EDA66.696C417AC60350CC0146E7912128FFC9025403F9&key=yt8&lang=zh",
          "code": "zh",
          "text": "Chinese"
        },
        {
          "url": "https://www.youtube.com/api/timedtext?v=AMCUqgu_cTM&ei=xeYOaN_bIvSKkucPp67p2A0&caps=asr&opi=112496729&exp=xpo&xoaf=7&hl=en&ip=0.0.0.0&ipbits=0&expire=1745832245&sparams=ip,ipbits,expire,v,ei,caps,opi,exp,xoaf&signature=1EF9185C6FCC876C2D3DA5D3B24C8E803D7EDA66.696C417AC60350CC0146E7912128FFC9025403F9&key=yt8&lang=zh-Hant",
          "code": "zh-Hant",
          "text": "Chinese (Traditional)"
        }
      ]
    }
取 zh 或者 en 的字幕作为返回值，url 里存储的是 xml 格式的字幕文本,其案例文本存放在 sample_files\API_resp\ytb_sample_timedtext.xml 里。
```

## 音视频转文字方法
参考 Client_Only 这个项目里的 client 模式转录文本的方法
- 传入音视频路径，等待该模块转写完毕会在原始文件同文件夹下面生成转录文本的地址（在原始文件夹下面）。提取 merge.txt 文本作为 transcript
- 这一步的转录时长和原始音视频时长有关系，请注意超时的问题。



# 其他要求
- 要有专门的测试脚本
    - 可以在不启动服务器的情况下直接测试某些 url 清单的转录效果。
    - 可以测试单个视频文件（指定文件路径）的转文字效果
- 要有一个企业微信的通知函数，任务进度更新或者错误的时候进行通知
  - 以 text 模式通知
  - 通知内容包含：原始链接、当前进行的步骤（获取下载地址？下载中？转录？）
  - webhook 地址写在 config 文件里。


# 增加功能-总结转录的文本
在转录完成 或者 获取到视频的字幕之后，调用大模型。两次调用大模型（并行同时调用），分别 校对文本 & 总结视频内容，然后发送到企业微信。
- 大模型的 api_key,calibrate_model,summary_model,base_url 都写在 config 文件里。
- 发送到企业微信时注意换行符，增加可读性。
- 可以命令行输入 txt 文本路径进行测试，比如 “output\bilibili_BV1JBLozmEFi_1745827015.merge.txt”

## 校对文本
calibrate_model: "gpt-4.1"
prompt:
```
你将收到一段音频的转录文本。你的任务是对这段文本进行校对,提高其可读性,但不改变原意。 请按照以下指示进行校对: 
1. 适当分段,使文本结构更清晰。每个自然段落应该是一个完整的思想单元。 
2. 修正明显的错别字和语法错误。 
3. 调整标点符号的使用,确保其正确性和一致性。 
4. 如有必要,可以轻微调整词序以提高可读性,但不要改变原意。 
5. 保留原文中的口语化表达和说话者的语气特点。 
6. 不要添加或删除任何实质性内容。 
7. 不要解释或评论文本内容。 

只返回校对后的文本,不要包含任何其他解释或评论。 
以下是需要校对的转录文本: 
<transcript>  </transcript>
```

## 总结文本
summary_model:"deepseek-chat"
prompt:
```
请以回车换行为分割，逐段地将正文内容，高度归纳提炼总结为凝炼的一句话，需涵盖主要内容，不能丢失关键信息和想表达的核心意思。用中文。然后将归纳总结的，用无序列表，挨个排列出来。
```

## 大模型 API 

示例请求
```
curl https://api.openai.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer api_key" \
  -d '{
        "model": "model",
        "messages": [
          {"role": "system", "content": "You are a helpful assistant."},
          {"role": "user", "content": "Hello!"}
        ],
        "stream": false
      }'
```

示例响应
```
{
    "id": "4411f908-f006-431e-b3c9-77a90222d94b",
    "object": "chat.completion",
    "created": 1745833820,
    "model": "deepseek-chat",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "你好，有什么能帮到你"
            },
            "logprobs": null,
            "finish_reason": "stop"
        }
    ],
    "usage": {
        "prompt_tokens": 1369,
        "completion_tokens": 586,
        "total_tokens": 1955,
        "prompt_tokens_details": {
            "cached_tokens": 1344
        },
        "prompt_cache_hit_tokens": 1344,
        "prompt_cache_miss_tokens": 25
    },
    "system_fingerprint": "fp_8802369eaa_prod0425fp8"
}
```
