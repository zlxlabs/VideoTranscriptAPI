# OpenAI compatibility

- On this page
- [Thinking](https://ai.google.dev/gemini-api/docs/openai#thinking)
- [Streaming](https://ai.google.dev/gemini-api/docs/openai#streaming)
- [Function calling](https://ai.google.dev/gemini-api/docs/openai#function-calling)
- [Image understanding](https://ai.google.dev/gemini-api/docs/openai#image-understanding)
- [Generate an image](https://ai.google.dev/gemini-api/docs/openai#generate-image)
- [Audio understanding](https://ai.google.dev/gemini-api/docs/openai#audio-understanding)
- [Structured output](https://ai.google.dev/gemini-api/docs/openai#structured-output)
- [Embeddings](https://ai.google.dev/gemini-api/docs/openai#embeddings)
- [Batch API](https://ai.google.dev/gemini-api/docs/openai#batch)
- [extra\_body](https://ai.google.dev/gemini-api/docs/openai#extra-body)
    - [cached\_content](https://ai.google.dev/gemini-api/docs/openai#cached-content)
- [List models](https://ai.google.dev/gemini-api/docs/openai#list-models)
- [Retrieve a model](https://ai.google.dev/gemini-api/docs/openai#retrieve-model)
- [Current limitations](https://ai.google.dev/gemini-api/docs/openai#current-limitations)
- [What's next](https://ai.google.dev/gemini-api/docs/openai#whats-next)

Gemini models are accessible using the OpenAI libraries (Python and TypeScript / Javascript) along with the REST API, by updating three lines of code and using your [Gemini API key](https://aistudio.google.com/apikey). If you aren't already using the OpenAI libraries, we recommend that you call the [Gemini API directly](https://ai.google.dev/gemini-api/docs/quickstart).

[Python](https://ai.google.dev/gemini-api/docs/openai#python)[JavaScript](https://ai.google.dev/gemini-api/docs/openai#javascript)[REST](https://ai.google.dev/gemini-api/docs/openai#rest) More

```
from openai import OpenAI

client = OpenAI(
    api_key="GEMINI_API_KEY",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

response = client.chat.completions.create(
    model="gemini-2.5-flash",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": "Explain to me how AI works"
        }
    ]
)

print(response.choices[0].message)
```

```
import OpenAI from "openai";

const openai = new OpenAI({
    apiKey: "GEMINI_API_KEY",
    baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/"
});

const response = await openai.chat.completions.create({
    model: "gemini-2.0-flash",
    messages: [
        { role: "system", content: "You are a helpful assistant." },
        {
            role: "user",
            content: "Explain to me how AI works",
        },
    ],
});

console.log(response.choices[0].message);
```

```
curl "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" \
-H "Content-Type: application/json" \
-H "Authorization: Bearer GEMINI_API_KEY" \
-d '{
    "model": "gemini-2.0-flash",
    "messages": [
        {"role": "user", "content": "Explain to me how AI works"}
    ]
    }'
```

What changed? Just three lines!

- **`api_key="GEMINI_API_KEY"`**: Replace "`GEMINI_API_KEY`" with your actual Gemini API key, which you can get in [Google AI Studio](https://aistudio.google.com/).
    
- **`base_url="https://generativelanguage.googleapis.com/v1beta/openai/"`:** This tells the OpenAI library to send requests to the Gemini API endpoint instead of the default URL.
    
- **`model="gemini-2.0-flash"`**: Choose a compatible Gemini model
    

## Thinking

Gemini 2.5 models are trained to think through complex problems, leading to significantly improved reasoning. The Gemini API comes with a ["thinking budget" parameter](https://ai.google.dev/gemini-api/docs/thinking#set-budget) which gives fine grain control over how much the model will think.

Unlike the Gemini API, the OpenAI API offers three levels of thinking control: `"low"`, `"medium"`, and `"high"`, which map to 1,024, 8,192, and 24,576 tokens, respectively.

If you want to disable thinking, you can set `reasoning_effort` to `"none"` (note that reasoning cannot be turned off for 2.5 Pro models).

[Python](https://ai.google.dev/gemini-api/docs/openai#python)[JavaScript](https://ai.google.dev/gemini-api/docs/openai#javascript)[REST](https://ai.google.dev/gemini-api/docs/openai#rest) More

```
from openai import OpenAI

client = OpenAI(
    api_key="GEMINI_API_KEY",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

response = client.chat.completions.create(
    model="gemini-2.5-flash",
    reasoning_effort="low",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": "Explain to me how AI works"
        }
    ]
)

print(response.choices[0].message)
```

```
import OpenAI from "openai";

const openai = new OpenAI({
    apiKey: "GEMINI_API_KEY",
    baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/"
});

const response = await openai.chat.completions.create({
    model: "gemini-2.5-flash",
    reasoning_effort: "low",
    messages: [
        { role: "system", content: "You are a helpful assistant." },
        {
            role: "user",
            content: "Explain to me how AI works",
        },
    ],
});

console.log(response.choices[0].message);
```

```
curl "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" \
-H "Content-Type: application/json" \
-H "Authorization: Bearer GEMINI_API_KEY" \
-d '{
    "model": "gemini-2.5-flash",
    "reasoning_effort": "low",
    "messages": [
        {"role": "user", "content": "Explain to me how AI works"}
      ]
    }'
```

Gemini thinking models also produce [thought summaries](https://ai.google.dev/gemini-api/docs/thinking#summaries) and can use exact [thinking budgets](https://ai.google.dev/gemini-api/docs/thinking#set-budget). You can use the [`extra_body`](https://ai.google.dev/gemini-api/docs/openai#extra-body) field to include these fields in your request.

Note that `reasoning_effort` and `thinking_budget` overlap functionality, so they can't be used at the same time.

[Python](https://ai.google.dev/gemini-api/docs/openai#python)[JavaScript](https://ai.google.dev/gemini-api/docs/openai#javascript)[REST](https://ai.google.dev/gemini-api/docs/openai#rest) More

```
from openai import OpenAI

client = OpenAI(
    api_key="GEMINI_API_KEY",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

response = client.chat.completions.create(
    model="gemini-2.5-flash",
    messages=[{"role": "user", "content": "Explain to me how AI works"}],
    extra_body={
      'extra_body': {
        "google": {
          "thinking_config": {
            "thinking_budget": 800,
            "include_thoughts": True
          }
        }
      }
    }
)

print(response.choices[0].message)
```

```
import OpenAI from "openai";

const openai = new OpenAI({
    apiKey: "GEMINI_API_KEY",
    baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/"
});

const response = await openai.chat.completions.create({
    model: "gemini-2.5-flash",
    messages: [{role: "user", content: "Explain to me how AI works",}],
    extra_body: {
      "google": {
        "thinking_config": {
          "thinking_budget": 800,
          "include_thoughts": true
        }
      }
    }
});

console.log(response.choices[0].message);
```

```
curl "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" \
-H "Content-Type: application/json" \
-H "Authorization: Bearer GEMINI_API_KEY" \
-d '{
    "model": "gemini-2.5-flash",
      "messages": [{"role": "user", "content": "Explain to me how AI works"}],
      "extra_body": {
        "google": {
           "thinking_config": {
             "include_thoughts": true
           }
        }
      }
    }'
```

## Streaming

The Gemini API supports [streaming responses](https://ai.google.dev/gemini-api/docs/text-generation?lang=python#generate-a-text-stream).

[Python](https://ai.google.dev/gemini-api/docs/openai#python)[JavaScript](https://ai.google.dev/gemini-api/docs/openai#javascript)[REST](https://ai.google.dev/gemini-api/docs/openai#rest) More

```
from openai import OpenAI

client = OpenAI(
    api_key="GEMINI_API_KEY",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

response = client.chat.completions.create(
  model="gemini-2.0-flash",
  messages=[
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  stream=True
)

for chunk in response:
    print(chunk.choices[0].delta)
```

```
import OpenAI from "openai";

const openai = new OpenAI({
    apiKey: "GEMINI_API_KEY",
    baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/"
});

async function main() {
  const completion = await openai.chat.completions.create({
    model: "gemini-2.0-flash",
    messages: [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Hello!"}
    ],
    stream: true,
  });

  for await (const chunk of completion) {
    console.log(chunk.choices[0].delta.content);
  }
}

main();
```

```
curl "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" \
-H "Content-Type: application/json" \
-H "Authorization: Bearer GEMINI_API_KEY" \
-d '{
    "model": "gemini-2.0-flash",
    "messages": [
        {"role": "user", "content": "Explain to me how AI works"}
    ],
    "stream": true
  }'
```


## Structured output

Gemini models can output JSON objects in any [structure you define](https://ai.google.dev/gemini-api/docs/structured-output).

[Python](https://ai.google.dev/gemini-api/docs/openai#python)[JavaScript](https://ai.google.dev/gemini-api/docs/openai#javascript) More

```
from pydantic import BaseModel
from openai import OpenAI

client = OpenAI(
    api_key="GEMINI_API_KEY",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

completion = client.beta.chat.completions.parse(
    model="gemini-2.0-flash",
    messages=[
        {"role": "system", "content": "Extract the event information."},
        {"role": "user", "content": "John and Susan are going to an AI conference on Friday."},
    ],
    response_format=CalendarEvent,
)

print(completion.choices[0].message.parsed)
```

```
import OpenAI from "openai";
import { zodResponseFormat } from "openai/helpers/zod";
import { z } from "zod";

const openai = new OpenAI({
    apiKey: "GEMINI_API_KEY",
    baseURL: "https://generativelanguage.googleapis.com/v1beta/openai"
});

const CalendarEvent = z.object({
  name: z.string(),
  date: z.string(),
  participants: z.array(z.string()),
});

const completion = await openai.chat.completions.parse({
  model: "gemini-2.0-flash",
  messages: [
    { role: "system", content: "Extract the event information." },
    { role: "user", content: "John and Susan are going to an AI conference on Friday" },
  ],
  response_format: zodResponseFormat(CalendarEvent, "event"),
});

const event = completion.choices[0].message.parsed;
console.log(event);
```

## Embeddings

Text embeddings measure the relatedness of text strings and can be generated using the [Gemini API](https://ai.google.dev/gemini-api/docs/embeddings).

[Python](https://ai.google.dev/gemini-api/docs/openai#python)[JavaScript](https://ai.google.dev/gemini-api/docs/openai#javascript)[REST](https://ai.google.dev/gemini-api/docs/openai#rest) More

```
from openai import OpenAI

client = OpenAI(
    api_key="GEMINI_API_KEY",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

response = client.embeddings.create(
    input="Your text string goes here",
    model="gemini-embedding-001"
)

print(response.data[0].embedding)
```

```
import OpenAI from "openai";

const openai = new OpenAI({
    apiKey: "GEMINI_API_KEY",
    baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/"
});

async function main() {
  const embedding = await openai.embeddings.create({
    model: "gemini-embedding-001",
    input: "Your text string goes here",
  });

  console.log(embedding);
}

main();
```

```
curl "https://generativelanguage.googleapis.com/v1beta/openai/embeddings" \
-H "Content-Type: application/json" \
-H "Authorization: Bearer GEMINI_API_KEY" \
-d '{
    "input": "Your text string goes here",
    "model": "gemini-embedding-001"
  }'
```

## Batch API

You can create [batch jobs](https://ai.google.dev/gemini-api/docs/batch-mode), submit them, and check their status using the OpenAI library.

You'll need to prepare the JSONL file in OpenAI input format. For example:

```
{"custom_id": "request-1", "method": "POST", "url": "/v1/chat/completions", "body": {"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "Tell me a one-sentence joke."}]}}
{"custom_id": "request-2", "method": "POST", "url": "/v1/chat/completions", "body": {"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "Why is the sky blue?"}]}}
```

OpenAI compatibility for Batch supports creating a batch, monitoring job status, and viewing batch results.

Compatibility for upload and download is currently not supported. Instead, the following example uses the `genai` client for uploading and downloading [files](https://ai.google.dev/gemini-api/docs/files), the same as when using the Gemini [Batch API](https://ai.google.dev/gemini-api/docs/batch-mode#input-file).

[Python](https://ai.google.dev/gemini-api/docs/openai#python) More

```
from openai import OpenAI

# Regular genai client for uploads & downloads
from google import genai
client = genai.Client()

openai_client = OpenAI(
    api_key="GEMINI_API_KEY",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

# Upload the JSONL file in OpenAI input format, using regular genai SDK
uploaded_file = client.files.upload(
    file='my-batch-requests.jsonl',
    config=types.UploadFileConfig(display_name='my-batch-requests', mime_type='jsonl')
)

# Create batch
batch = openai_client.batches.create(
    input_file_id=batch_input_file_id,
    endpoint="/v1/chat/completions",
    completion_window="24h"
)

# Wait for batch to finish (up to 24h)
while True:
    batch = client.batches.retrieve(batch.id)
    if batch.status in ('completed', 'failed', 'cancelled', 'expired'):
        break
    print(f"Batch not finished. Current state: {batch.status}. Waiting 30 seconds...")
    time.sleep(30)
print(f"Batch finished: {batch}")

# Download results in OpenAI output format, using regular genai SDK
file_content = genai_client.files.download(file=batch.output_file_id).decode('utf-8')

# See batch_output JSONL in OpenAI output format
for line in file_content.splitlines():
    print(line)    
```    

The OpenAI SDK also supports [generating embeddings with the Batch API](https://ai.google.dev/gemini-api/docs/batch-api#batch-embeddings). To do so, switch out the `create` method's `endpoint` field for an embeddings endpoint, as well as the `url` and `model` keys in the JSONL file:

```
# JSONL file using embeddings model and endpoint
# {"custom_id": "request-1", "method": "POST", "url": "/v1/embeddings", "body": {"model": "ggemini-embedding-001", "messages": [{"role": "user", "content": "Tell me a one-sentence joke."}]}}
# {"custom_id": "request-2", "method": "POST", "url": "/v1/embeddings", "body": {"model": "gemini-embedding-001", "messages": [{"role": "user", "content": "Why is the sky blue?"}]}}

# ...

# Create batch step with embeddings endpoint
batch = openai_client.batches.create(
    input_file_id=batch_input_file_id,
    endpoint="/v1/embeddings",
    completion_window="24h"
)
```

See the [Batch embedding generation](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_OpenAI_Compatibility.ipynb) section of the OpenAI compatibility cookbook for a complete example.

## `extra_body`

There are several features supported by Gemini that are not available in OpenAI models but can be enabled using the `extra_body` field.

**`extra_body` features**

<table><tbody><tr><td><code translate="no" dir="ltr">cached_<wbr>content</code></td><td>Corresponds to Gemini's <code translate="no" dir="ltr">Generate<wbr>Content<wbr>Request.<wbr>cached_<wbr>content</code>.</td></tr><tr><td><code translate="no" dir="ltr">thinking_<wbr>config</code></td><td>Corresponds to Gemini's <code translate="no" dir="ltr">Thinking<wbr>Config</code>.</td></tr></tbody></table>

### `cached_content`

Here's an example of using `extra_body` to set `cached_content`:

[Python](https://ai.google.dev/gemini-api/docs/openai#python) More

```
from openai import OpenAI

client = OpenAI(
    api_key=MY_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/"
)

stream = client.chat.completions.create(
    model="gemini-2.5-pro",
    n=1,
    messages=[
        {
            "role": "user",
            "content": "Summarize the video"
        }
    ],
    stream=True,
    stream_options={'include_usage': True},
    extra_body={
        'extra_body':
        {
            'google': {
              'cached_content': "cachedContents/0000aaaa1111bbbb2222cccc3333dddd4444eeee"
          }
        }
    }
)

for chunk in stream:
    print(chunk)
    print(chunk.usage.to_dict())
```

## List models

Get a list of available Gemini models:

[Python](https://ai.google.dev/gemini-api/docs/openai#python)[JavaScript](https://ai.google.dev/gemini-api/docs/openai#javascript)[REST](https://ai.google.dev/gemini-api/docs/openai#rest) More

```
from openai import OpenAI

client = OpenAI(
  api_key="GEMINI_API_KEY",
  base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

models = client.models.list()
for model in models:
  print(model.id)
```

```
import OpenAI from "openai";

const openai = new OpenAI({
  apiKey: "GEMINI_API_KEY",
  baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
});

async function main() {
  const list = await openai.models.list();

  for await (const model of list) {
    console.log(model);
  }
}
main();
```

```
curl https://generativelanguage.googleapis.com/v1beta/openai/models \
-H "Authorization: Bearer GEMINI_API_KEY"
```

## Retrieve a model

Retrieve a Gemini model:

[Python](https://ai.google.dev/gemini-api/docs/openai#python)[JavaScript](https://ai.google.dev/gemini-api/docs/openai#javascript)[REST](https://ai.google.dev/gemini-api/docs/openai#rest) More

```
from openai import OpenAI

client = OpenAI(
  api_key="GEMINI_API_KEY",
  base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

model = client.models.retrieve("gemini-2.0-flash")
print(model.id)
```

```
import OpenAI from "openai";

const openai = new OpenAI({
  apiKey: "GEMINI_API_KEY",
  baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
});

async function main() {
  const model = await openai.models.retrieve("gemini-2.0-flash");
  console.log(model.id);
}

main();
```

```
curl https://generativelanguage.googleapis.com/v1beta/openai/models/gemini-2.0-flash \
-H "Authorization: Bearer GEMINI_API_KEY"
```

## Current limitations

Support for the OpenAI libraries is still in beta while we extend feature support.

If you have questions about supported parameters, upcoming features, or run into any issues getting started with Gemini, join our [Developer Forum](https://discuss.ai.google.dev/c/gemini-api/4).

## What's next

Try our [OpenAI Compatibility Colab](https://colab.sandbox.google.com/github/google-gemini/cookbook/blob/main/quickstarts/Get_started_OpenAI_Compatibility.ipynb) to work through more detailed examples.