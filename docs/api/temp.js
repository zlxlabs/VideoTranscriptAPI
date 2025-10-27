var api_base_url = arg1;
var api_key = arg2;
var share_text = arg3;
var webhook_url = arg4;
var use_speaker_recognition = arg5;

var httpRequest = new XMLHttpRequest();
httpRequest.open("POST", "https://summary.lexgogo.site/api/transcribe", true);
httpRequest.setRequestHeader("Content-type", "application/json");
httpRequest.setRequestHeader("Authorization", "Bearer " + api_key);
var obj = { url: share_text, use_speaker_recognition: use_speaker_recognition, wechat_webhook: webhook_url };
httpRequest.send(JSON.stringify(obj));