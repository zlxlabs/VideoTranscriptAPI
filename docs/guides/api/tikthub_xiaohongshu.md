# 获取视频笔记信息 V1/ Get video note info V1

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/v1/xiaohongshu/app/get_video_note_info:
    get:
      summary: 获取视频笔记信息 V1/ Get video note info V1
      deprecated: false
      description: >-
        # [中文]

        ### 用途:

        - 获取视频笔记信息 V1

        - 视频笔记用这个接口，成功率高。

        ### 参数:

        - note_id: 笔记ID，可以从小红书的分享链接中获取

        - share_text: 小红书分享链接（支持APP和Web端分享链接）

        - 优先使用`note_id`，如果没有则使用`share_text`，两个参数二选一，如都携带则以`note_id`为准。

        ### 返回:

        - 笔记详情数据，包含以下主要字段：
            - note_id: 笔记ID
            - title: 笔记标题
            - desc: 笔记内容描述
            - type: 笔记类型（normal=图文笔记，video=视频笔记）
            - user: 作者信息对象
                - user_id: 用户ID
                - nickname: 用户昵称
                - avatar: 用户头像URL
            - image_list: 图片列表（图文笔记）
            - video_info: 视频信息（视频笔记）
            - interact_info: 互动数据
                - liked_count: 点赞数
                - collected_count: 收藏数
                - comment_count: 评论数
                - share_count: 分享数
            - tag_list: 话题标签列表
            - time: 发布时间戳
            - ip_location: IP属地

        # [English]

        ### Purpose:

        - Get video note info V1

        - Use this interface for video notes, higher success rate.

        ### Parameters:

        - note_id: Note ID, can be obtained from the sharing link of Xiaohongshu
        website.

        - share_text: Xiaohongshu sharing link (support APP and Web sharing
        link)

        - Prefer to use `note_id`, if not, use `share_text`, one of the two
        parameters is required, if both are carried, `note_id` shall prevail.

        ### Return:

        - Note detail data with main fields:
            - note_id: Note ID
            - title: Note title
            - desc: Note content description
            - type: Note type (normal=image note, video=video note)
            - user: Author info object
                - user_id: User ID
                - nickname: User nickname
                - avatar: User avatar URL
            - image_list: Image list (for image notes)
            - video_info: Video info (for video notes)
            - interact_info: Interaction data
                - liked_count: Like count
                - collected_count: Collect count
                - comment_count: Comment count
                - share_count: Share count
            - tag_list: Topic tag list
            - time: Publish timestamp
            - ip_location: IP location

        # [示例/Example]

        note_id="681b87cd0000000022027853"
      operationId: get_video_note_info_api_v1_xiaohongshu_app_get_video_note_info_get
      tags:
        - Xiaohongshu-App-API
        - Xiaohongshu-App-API
      parameters:
        - name: note_id
          in: query
          description: 笔记ID/Note ID
          required: false
          example: 681b87cd0000000022027853
          schema:
            type: string
            description: 笔记ID/Note ID
            default: ''
            title: Note Id
        - name: share_text
          in: query
          description: 分享链接/Share link
          required: false
          example: https://xhslink.com/a/EZ4M9TwMA6c3
          schema:
            type: string
            description: 分享链接/Share link
            default: ''
            title: Share Text
      responses:
        '200':
          description: Successful Response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ResponseModel'
          headers: {}
          x-apifox-name: OK
        '422':
          description: Validation Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/HTTPValidationError'
          headers: {}
          x-apifox-name: Parameter Error
      security:
        - HTTPBearer: []
          x-apifox:
            schemeGroups:
              - id: uV0YdY6DBnj9M4Fx1Onyu
                schemeIds:
                  - HTTPBearer
            required: true
            use:
              id: uV0YdY6DBnj9M4Fx1Onyu
            scopes:
              uV0YdY6DBnj9M4Fx1Onyu:
                HTTPBearer: []
      x-apifox-folder: Xiaohongshu-App-API
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/4705614/apis/api-334614202-run
components:
  schemas:
    ResponseModel:
      properties:
        code:
          type: integer
          title: Code
          description: HTTP status code | HTTP状态码
          default: 200
        request_id:
          anyOf:
            - type: string
            - type: 'null'
          title: Request Id
          description: Unique request identifier | 唯一请求标识符
        message:
          type: string
          title: Message
          description: Response message (EN-US) | 响应消息 (English)
          default: Request successful. This request will incur a charge.
        message_zh:
          type: string
          title: Message Zh
          description: Response message (ZH-CN) | 响应消息 (中文)
          default: 请求成功，本次请求将被计费。
        support:
          type: string
          title: Support
          description: Support message | 支持消息
          default: 'Discord: https://discord.gg/aMEAS8Xsvz'
        time:
          type: string
          title: Time
          description: The time the response was generated | 生成响应的时间
        time_stamp:
          type: integer
          title: Time Stamp
          description: The timestamp the response was generated | 生成响应的时间戳
        time_zone:
          type: string
          title: Time Zone
          description: The timezone of the response time | 响应时间的时区
          default: America/Los_Angeles
        docs:
          anyOf:
            - type: string
            - type: 'null'
          title: Docs
          description: >-
            Link to the API Swagger documentation for this endpoint | 此端点的 API
            Swagger 文档链接
        cache_message:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message
          description: Cache message (EN-US) | 缓存消息 (English)
          default: >-
            This request will be cached. You can access the cached result
            directly using the URL below, valid for 24 hours. Accessing the
            cache will not incur additional charges.
        cache_message_zh:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message Zh
          description: Cache message (ZH-CN) | 缓存消息 (中文)
          default: 本次请求将被缓存，你可以使用下面的 URL 直接访问缓存结果，有效期为 24 小时，访问缓存不会产生额外费用。
        cache_url:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Url
          description: The URL to access the cached result | 访问缓存结果的 URL
        router:
          type: string
          title: Router
          description: The endpoint that generated this response | 生成此响应的端点
          default: ''
        params:
          type: string
        data:
          anyOf:
            - type: string
            - type: 'null'
          title: Data
          description: The response data | 响应数据
      type: object
      title: ResponseModel
      x-apifox-orders:
        - code
        - request_id
        - message
        - message_zh
        - support
        - time
        - time_stamp
        - time_zone
        - docs
        - cache_message
        - cache_message_zh
        - cache_url
        - router
        - params
        - data
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    HTTPValidationError:
      properties:
        detail:
          items:
            $ref: '#/components/schemas/ValidationError'
          type: array
          title: Detail
      type: object
      title: HTTPValidationError
      x-apifox-orders:
        - detail
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    ValidationError:
      properties:
        loc:
          items:
            anyOf:
              - type: string
              - type: integer
          type: array
          title: Location
        msg:
          type: string
          title: Message
        type:
          type: string
          title: Error Type
      type: object
      required:
        - loc
        - msg
        - type
      title: ValidationError
      x-apifox-orders:
        - loc
        - msg
        - type
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
  securitySchemes:
    Bearer Token:
      type: bearer
      scheme: bearer
    HTTPBearer:
      type: bearer
      description: >
        ----

        #### API Token Introduction:

        ##### Method 1: Use API Token in the Request Header (Recommended)

        - **Header**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: Click on the `Authorize` button in the upper right
        corner of the page to enter the API token directly without the `Bearer`
        keyword.


        ##### Method 2: Use API Token in the Cookie (Not Recommended, Use Only
        When Method 1 is Unavailable)

        - **Cookie**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `Authorization=Bearer your_token`


        #### Get API Token:

        1. Register and log in to your account on the TikHub website.

        2. Go to the user center, click on the API token menu, and create an API
        token.

        3. Copy and use the API token in the request header.

        4. Keep your API token confidential and use it only in the request
        header.


        ----


        #### API令牌简介:

        ##### 方法一：在请求头中使用API令牌（推荐）

        - **请求头**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: 点击页面右上角的`Authorize`按钮，直接输入API令牌，不需要`Bearer`关键字。


        ##### 方法二：在Cookie中使用API令牌（不推荐，仅在无法使用方法一时使用）

        - **Cookie**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `Authorization=Bearer your_token`


        #### 获取API令牌:

        1. 在TikHub网站注册并登录账户。

        2. 进入用户中心，点击API令牌菜单，创建API令牌。

        3. 复制并在请求头中使用API令牌。

        4. 保密您的API令牌，仅在请求头中使用。
      scheme: bearer
servers:
  - url: https://api.tikhub.io
    description: Production Environment
security: []

```

# 获取笔记信息 V1/Get note info V1

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/v1/xiaohongshu/app/get_note_info:
    get:
      summary: 获取笔记信息 V1/Get note info V1
      deprecated: false
      description: >-
        # [中文]

        ### 用途:

        - 获取笔记信息 V1

        ### 参数:

        - note_id: 笔记ID，可以从小红书的分享链接中获取

        - share_text: 小红书分享链接（支持APP和Web端分享链接）

        - force_video_enabled: 是否是视频笔记，默认为False

        - 优先使用`note_id`，如果没有则使用`share_text`，两个参数二选一，如都携带则以`note_id`为准。

        ### 返回:

        - 笔记详情数据，包含以下主要字段：
            - note_id: 笔记ID
            - title: 笔记标题
            - desc: 笔记内容描述
            - type: 笔记类型（normal=图文笔记，video=视频笔记）
            - user: 作者信息对象
                - user_id: 用户ID
                - nickname: 用户昵称
                - avatar: 用户头像URL
            - image_list: 图片列表（图文笔记）
            - video_info: 视频信息（视频笔记）
            - interact_info: 互动数据
                - liked_count: 点赞数
                - collected_count: 收藏数
                - comment_count: 评论数
                - share_count: 分享数
            - tag_list: 话题标签列表
            - time: 发布时间戳
            - ip_location: IP属地

        # [English]

        ### Purpose:

        - Get note info V1

        ### Parameters:

        - note_id: Note ID, can be obtained from the sharing link of Xiaohongshu
        website.

        - share_text: Xiaohongshu sharing link (support APP and Web sharing
        link)

        - force_video_enabled: Is video note, default is False

        - Prefer to use `note_id`, if not, use `share_text`, one of the two
        parameters is required, if both are carried, `note_id` shall prevail.

        ### Return:

        - Note detail data with main fields:
            - note_id: Note ID
            - title: Note title
            - desc: Note content description
            - type: Note type (normal=image note, video=video note)
            - user: Author info object
                - user_id: User ID
                - nickname: User nickname
                - avatar: User avatar URL
            - image_list: Image list (for image notes)
            - video_info: Video info (for video notes)
            - interact_info: Interaction data
                - liked_count: Like count
                - collected_count: Collect count
                - comment_count: Comment count
                - share_count: Share count
            - tag_list: Topic tag list
            - time: Publish timestamp
            - ip_location: IP location

        # [示例/Example]

        note_id="665f95200000000006005624"
      operationId: get_note_info_v1_api_v1_xiaohongshu_app_get_note_info_get
      tags:
        - Xiaohongshu-App-API
        - Xiaohongshu-App-API
      parameters:
        - name: note_id
          in: query
          description: 笔记ID/Note ID
          required: false
          example: 665f95200000000006005624
          schema:
            type: string
            description: 笔记ID/Note ID
            default: ''
            title: Note Id
        - name: share_text
          in: query
          description: 分享链接/Share link
          required: false
          example: https://xhslink.com/a/EZ4M9TwMA6c3
          schema:
            type: string
            description: 分享链接/Share link
            default: ''
            title: Share Text
        - name: force_video_enabled
          in: query
          description: 是否是视频笔记/Is video note
          required: false
          example: 'false'
          schema:
            type: boolean
            description: 是否是视频笔记/Is video note
            default: false
            title: Force Video Enabled
      responses:
        '200':
          description: Successful Response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ResponseModel'
          headers: {}
          x-apifox-name: OK
        '422':
          description: Validation Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/HTTPValidationError'
          headers: {}
          x-apifox-name: Parameter Error
      security:
        - HTTPBearer: []
          x-apifox:
            schemeGroups:
              - id: r3bQ5canhWmzTBYGndExB
                schemeIds:
                  - HTTPBearer
            required: true
            use:
              id: r3bQ5canhWmzTBYGndExB
            scopes:
              r3bQ5canhWmzTBYGndExB:
                HTTPBearer: []
      x-apifox-folder: Xiaohongshu-App-API
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/4705614/apis/api-310965839-run
components:
  schemas:
    ResponseModel:
      properties:
        code:
          type: integer
          title: Code
          description: HTTP status code | HTTP状态码
          default: 200
        request_id:
          anyOf:
            - type: string
            - type: 'null'
          title: Request Id
          description: Unique request identifier | 唯一请求标识符
        message:
          type: string
          title: Message
          description: Response message (EN-US) | 响应消息 (English)
          default: Request successful. This request will incur a charge.
        message_zh:
          type: string
          title: Message Zh
          description: Response message (ZH-CN) | 响应消息 (中文)
          default: 请求成功，本次请求将被计费。
        support:
          type: string
          title: Support
          description: Support message | 支持消息
          default: 'Discord: https://discord.gg/aMEAS8Xsvz'
        time:
          type: string
          title: Time
          description: The time the response was generated | 生成响应的时间
        time_stamp:
          type: integer
          title: Time Stamp
          description: The timestamp the response was generated | 生成响应的时间戳
        time_zone:
          type: string
          title: Time Zone
          description: The timezone of the response time | 响应时间的时区
          default: America/Los_Angeles
        docs:
          anyOf:
            - type: string
            - type: 'null'
          title: Docs
          description: >-
            Link to the API Swagger documentation for this endpoint | 此端点的 API
            Swagger 文档链接
        cache_message:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message
          description: Cache message (EN-US) | 缓存消息 (English)
          default: >-
            This request will be cached. You can access the cached result
            directly using the URL below, valid for 24 hours. Accessing the
            cache will not incur additional charges.
        cache_message_zh:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message Zh
          description: Cache message (ZH-CN) | 缓存消息 (中文)
          default: 本次请求将被缓存，你可以使用下面的 URL 直接访问缓存结果，有效期为 24 小时，访问缓存不会产生额外费用。
        cache_url:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Url
          description: The URL to access the cached result | 访问缓存结果的 URL
        router:
          type: string
          title: Router
          description: The endpoint that generated this response | 生成此响应的端点
          default: ''
        params:
          type: string
        data:
          anyOf:
            - type: string
            - type: 'null'
          title: Data
          description: The response data | 响应数据
      type: object
      title: ResponseModel
      x-apifox-orders:
        - code
        - request_id
        - message
        - message_zh
        - support
        - time
        - time_stamp
        - time_zone
        - docs
        - cache_message
        - cache_message_zh
        - cache_url
        - router
        - params
        - data
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    HTTPValidationError:
      properties:
        detail:
          items:
            $ref: '#/components/schemas/ValidationError'
          type: array
          title: Detail
      type: object
      title: HTTPValidationError
      x-apifox-orders:
        - detail
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    ValidationError:
      properties:
        loc:
          items:
            anyOf:
              - type: string
              - type: integer
          type: array
          title: Location
        msg:
          type: string
          title: Message
        type:
          type: string
          title: Error Type
      type: object
      required:
        - loc
        - msg
        - type
      title: ValidationError
      x-apifox-orders:
        - loc
        - msg
        - type
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
  securitySchemes:
    Bearer Token:
      type: bearer
      scheme: bearer
    HTTPBearer:
      type: bearer
      description: >
        ----

        #### API Token Introduction:

        ##### Method 1: Use API Token in the Request Header (Recommended)

        - **Header**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: Click on the `Authorize` button in the upper right
        corner of the page to enter the API token directly without the `Bearer`
        keyword.


        ##### Method 2: Use API Token in the Cookie (Not Recommended, Use Only
        When Method 1 is Unavailable)

        - **Cookie**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `Authorization=Bearer your_token`


        #### Get API Token:

        1. Register and log in to your account on the TikHub website.

        2. Go to the user center, click on the API token menu, and create an API
        token.

        3. Copy and use the API token in the request header.

        4. Keep your API token confidential and use it only in the request
        header.


        ----


        #### API令牌简介:

        ##### 方法一：在请求头中使用API令牌（推荐）

        - **请求头**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: 点击页面右上角的`Authorize`按钮，直接输入API令牌，不需要`Bearer`关键字。


        ##### 方法二：在Cookie中使用API令牌（不推荐，仅在无法使用方法一时使用）

        - **Cookie**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `Authorization=Bearer your_token`


        #### 获取API令牌:

        1. 在TikHub网站注册并登录账户。

        2. 进入用户中心，点击API令牌菜单，创建API令牌。

        3. 复制并在请求头中使用API令牌。

        4. 保密您的API令牌，仅在请求头中使用。
      scheme: bearer
servers:
  - url: https://api.tikhub.io
    description: Production Environment
security: []

```

# 获取笔记信息 V2/Get note info V2

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/v1/xiaohongshu/web/get_note_info_v2:
    get:
      summary: 获取笔记信息 V2/Get note info V2
      deprecated: false
      description: >-
        # [中文]

        ### 用途:

        - 获取笔记信息 V2

        ### 参数:

        - note_id: 笔记ID，可以从小红书的分享链接中获取

        - share_text: 小红书分享链接（支持APP和Web端分享链接）

        - 优先使用`note_id`，如果没有则使用`share_text`，两个参数二选一，如都携带则以`note_id`为准。

        ### 返回:

        - 笔记信息


        # [English]

        ### Purpose:

        - Get note info V2

        ### Parameters:

        - note_id: Note ID, can be obtained from the sharing link of Xiaohongshu
        website.

        - share_text: Xiaohongshu sharing link (support APP and Web sharing
        link)

        - Prefer to use `note_id`, if not, use `share_text`, one of the two
        parameters is required, if both are carried, `note_id` shall prevail.

        ### Return:

        - Note info


        # [示例/Example]

        note_id="665f95200000000006005624"
      operationId: get_note_info_v2_api_v1_xiaohongshu_web_get_note_info_v2_get
      tags:
        - Xiaohongshu-Web-API
        - Xiaohongshu-Web-API
      parameters:
        - name: note_id
          in: query
          description: 笔记ID/Note ID
          required: false
          example: 665f95200000000006005624
          schema:
            type: string
            description: 笔记ID/Note ID
            default: ''
            title: Note Id
        - name: share_text
          in: query
          description: 分享链接/Share link
          required: false
          example: https://xhslink.com/a/EZ4M9TwMA6c3
          schema:
            type: string
            description: 分享链接/Share link
            default: ''
            title: Share Text
      responses:
        '200':
          description: Successful Response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ResponseModel'
          headers: {}
          x-apifox-name: OK
        '422':
          description: Validation Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/HTTPValidationError'
          headers: {}
          x-apifox-name: Parameter Error
      security:
        - HTTPBearer: []
          x-apifox:
            schemeGroups:
              - id: U6d3B4uOuZZg3QMX8Ahrr
                schemeIds:
                  - HTTPBearer
            required: true
            use:
              id: U6d3B4uOuZZg3QMX8Ahrr
            scopes:
              U6d3B4uOuZZg3QMX8Ahrr:
                HTTPBearer: []
      x-apifox-folder: Xiaohongshu-Web-API
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/4705614/apis/api-207023656-run
components:
  schemas:
    ResponseModel:
      properties:
        code:
          type: integer
          title: Code
          description: HTTP status code | HTTP状态码
          default: 200
        request_id:
          anyOf:
            - type: string
            - type: 'null'
          title: Request Id
          description: Unique request identifier | 唯一请求标识符
        message:
          type: string
          title: Message
          description: Response message (EN-US) | 响应消息 (English)
          default: Request successful. This request will incur a charge.
        message_zh:
          type: string
          title: Message Zh
          description: Response message (ZH-CN) | 响应消息 (中文)
          default: 请求成功，本次请求将被计费。
        support:
          type: string
          title: Support
          description: Support message | 支持消息
          default: 'Discord: https://discord.gg/aMEAS8Xsvz'
        time:
          type: string
          title: Time
          description: The time the response was generated | 生成响应的时间
        time_stamp:
          type: integer
          title: Time Stamp
          description: The timestamp the response was generated | 生成响应的时间戳
        time_zone:
          type: string
          title: Time Zone
          description: The timezone of the response time | 响应时间的时区
          default: America/Los_Angeles
        docs:
          anyOf:
            - type: string
            - type: 'null'
          title: Docs
          description: >-
            Link to the API Swagger documentation for this endpoint | 此端点的 API
            Swagger 文档链接
        cache_message:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message
          description: Cache message (EN-US) | 缓存消息 (English)
          default: >-
            This request will be cached. You can access the cached result
            directly using the URL below, valid for 24 hours. Accessing the
            cache will not incur additional charges.
        cache_message_zh:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message Zh
          description: Cache message (ZH-CN) | 缓存消息 (中文)
          default: 本次请求将被缓存，你可以使用下面的 URL 直接访问缓存结果，有效期为 24 小时，访问缓存不会产生额外费用。
        cache_url:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Url
          description: The URL to access the cached result | 访问缓存结果的 URL
        router:
          type: string
          title: Router
          description: The endpoint that generated this response | 生成此响应的端点
          default: ''
        params:
          type: string
        data:
          anyOf:
            - type: string
            - type: 'null'
          title: Data
          description: The response data | 响应数据
      type: object
      title: ResponseModel
      x-apifox-orders:
        - code
        - request_id
        - message
        - message_zh
        - support
        - time
        - time_stamp
        - time_zone
        - docs
        - cache_message
        - cache_message_zh
        - cache_url
        - router
        - params
        - data
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    HTTPValidationError:
      properties:
        detail:
          items:
            $ref: '#/components/schemas/ValidationError'
          type: array
          title: Detail
      type: object
      title: HTTPValidationError
      x-apifox-orders:
        - detail
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    ValidationError:
      properties:
        loc:
          items:
            anyOf:
              - type: string
              - type: integer
          type: array
          title: Location
        msg:
          type: string
          title: Message
        type:
          type: string
          title: Error Type
      type: object
      required:
        - loc
        - msg
        - type
      title: ValidationError
      x-apifox-orders:
        - loc
        - msg
        - type
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
  securitySchemes:
    Bearer Token:
      type: bearer
      scheme: bearer
    HTTPBearer:
      type: bearer
      description: >
        ----

        #### API Token Introduction:

        ##### Method 1: Use API Token in the Request Header (Recommended)

        - **Header**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: Click on the `Authorize` button in the upper right
        corner of the page to enter the API token directly without the `Bearer`
        keyword.


        ##### Method 2: Use API Token in the Cookie (Not Recommended, Use Only
        When Method 1 is Unavailable)

        - **Cookie**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `Authorization=Bearer your_token`


        #### Get API Token:

        1. Register and log in to your account on the TikHub website.

        2. Go to the user center, click on the API token menu, and create an API
        token.

        3. Copy and use the API token in the request header.

        4. Keep your API token confidential and use it only in the request
        header.


        ----


        #### API令牌简介:

        ##### 方法一：在请求头中使用API令牌（推荐）

        - **请求头**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: 点击页面右上角的`Authorize`按钮，直接输入API令牌，不需要`Bearer`关键字。


        ##### 方法二：在Cookie中使用API令牌（不推荐，仅在无法使用方法一时使用）

        - **Cookie**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `Authorization=Bearer your_token`


        #### 获取API令牌:

        1. 在TikHub网站注册并登录账户。

        2. 进入用户中心，点击API令牌菜单，创建API令牌。

        3. 复制并在请求头中使用API令牌。

        4. 保密您的API令牌，仅在请求头中使用。
      scheme: bearer
servers:
  - url: https://api.tikhub.io
    description: Production Environment
security: []

```

# 获取笔记信息 V4/Get note info V4

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/v1/xiaohongshu/web/get_note_info_v4:
    get:
      summary: 获取笔记信息 V4/Get note info V4
      deprecated: false
      description: >-
        # [中文]

        ### 用途:

        - 获取笔记信息V4

        ### 参数:

        - note_id: 笔记ID，可以从小红书的分享链接中获取

        - share_text: 小红书分享链接（支持APP和Web端分享链接）

        - 优先使用`note_id`，如果没有则使用`share_text`，两个参数二选一，如都携带则以`note_id`为准。

        ### 返回:

        - 笔记信息


        # [English]

        ### Purpose:

        - Get note info V4

        ### Parameters:

        - note_id: Note ID, can be obtained from the sharing link of Xiaohongshu
        website.

        - share_text: Xiaohongshu sharing link (support APP and Web sharing
        link)

        - Prefer to use `note_id`, if not, use `share_text`, one of the two
        parameters is required, if both are carried, `note_id` shall prevail.

        ### Return:

        - Note info


        # [示例/Example]

        note_id="665f95200000000006005624"
      operationId: get_note_info_v4_api_v1_xiaohongshu_web_get_note_info_v4_get
      tags:
        - Xiaohongshu-Web-API
        - Xiaohongshu-Web-API
      parameters:
        - name: note_id
          in: query
          description: 笔记ID/Note ID
          required: false
          example: 665f95200000000006005624
          schema:
            type: string
            description: 笔记ID/Note ID
            default: ''
            title: Note Id
        - name: share_text
          in: query
          description: 分享链接/Share link
          required: false
          example: https://xhslink.com/a/EZ4M9TwMA6c3
          schema:
            type: string
            description: 分享链接/Share link
            default: ''
            title: Share Text
      responses:
        '200':
          description: Successful Response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ResponseModel'
          headers: {}
          x-apifox-name: OK
        '422':
          description: Validation Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/HTTPValidationError'
          headers: {}
          x-apifox-name: Parameter Error
      security:
        - HTTPBearer: []
          x-apifox:
            schemeGroups:
              - id: Vqxbw9mhhohOnUtt6es8m
                schemeIds:
                  - HTTPBearer
            required: true
            use:
              id: Vqxbw9mhhohOnUtt6es8m
            scopes:
              Vqxbw9mhhohOnUtt6es8m:
                HTTPBearer: []
      x-apifox-folder: Xiaohongshu-Web-API
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/4705614/apis/api-255542734-run
components:
  schemas:
    ResponseModel:
      properties:
        code:
          type: integer
          title: Code
          description: HTTP status code | HTTP状态码
          default: 200
        request_id:
          anyOf:
            - type: string
            - type: 'null'
          title: Request Id
          description: Unique request identifier | 唯一请求标识符
        message:
          type: string
          title: Message
          description: Response message (EN-US) | 响应消息 (English)
          default: Request successful. This request will incur a charge.
        message_zh:
          type: string
          title: Message Zh
          description: Response message (ZH-CN) | 响应消息 (中文)
          default: 请求成功，本次请求将被计费。
        support:
          type: string
          title: Support
          description: Support message | 支持消息
          default: 'Discord: https://discord.gg/aMEAS8Xsvz'
        time:
          type: string
          title: Time
          description: The time the response was generated | 生成响应的时间
        time_stamp:
          type: integer
          title: Time Stamp
          description: The timestamp the response was generated | 生成响应的时间戳
        time_zone:
          type: string
          title: Time Zone
          description: The timezone of the response time | 响应时间的时区
          default: America/Los_Angeles
        docs:
          anyOf:
            - type: string
            - type: 'null'
          title: Docs
          description: >-
            Link to the API Swagger documentation for this endpoint | 此端点的 API
            Swagger 文档链接
        cache_message:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message
          description: Cache message (EN-US) | 缓存消息 (English)
          default: >-
            This request will be cached. You can access the cached result
            directly using the URL below, valid for 24 hours. Accessing the
            cache will not incur additional charges.
        cache_message_zh:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message Zh
          description: Cache message (ZH-CN) | 缓存消息 (中文)
          default: 本次请求将被缓存，你可以使用下面的 URL 直接访问缓存结果，有效期为 24 小时，访问缓存不会产生额外费用。
        cache_url:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Url
          description: The URL to access the cached result | 访问缓存结果的 URL
        router:
          type: string
          title: Router
          description: The endpoint that generated this response | 生成此响应的端点
          default: ''
        params:
          type: string
        data:
          anyOf:
            - type: string
            - type: 'null'
          title: Data
          description: The response data | 响应数据
      type: object
      title: ResponseModel
      x-apifox-orders:
        - code
        - request_id
        - message
        - message_zh
        - support
        - time
        - time_stamp
        - time_zone
        - docs
        - cache_message
        - cache_message_zh
        - cache_url
        - router
        - params
        - data
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    HTTPValidationError:
      properties:
        detail:
          items:
            $ref: '#/components/schemas/ValidationError'
          type: array
          title: Detail
      type: object
      title: HTTPValidationError
      x-apifox-orders:
        - detail
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    ValidationError:
      properties:
        loc:
          items:
            anyOf:
              - type: string
              - type: integer
          type: array
          title: Location
        msg:
          type: string
          title: Message
        type:
          type: string
          title: Error Type
      type: object
      required:
        - loc
        - msg
        - type
      title: ValidationError
      x-apifox-orders:
        - loc
        - msg
        - type
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
  securitySchemes:
    Bearer Token:
      type: bearer
      scheme: bearer
    HTTPBearer:
      type: bearer
      description: >
        ----

        #### API Token Introduction:

        ##### Method 1: Use API Token in the Request Header (Recommended)

        - **Header**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: Click on the `Authorize` button in the upper right
        corner of the page to enter the API token directly without the `Bearer`
        keyword.


        ##### Method 2: Use API Token in the Cookie (Not Recommended, Use Only
        When Method 1 is Unavailable)

        - **Cookie**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `Authorization=Bearer your_token`


        #### Get API Token:

        1. Register and log in to your account on the TikHub website.

        2. Go to the user center, click on the API token menu, and create an API
        token.

        3. Copy and use the API token in the request header.

        4. Keep your API token confidential and use it only in the request
        header.


        ----


        #### API令牌简介:

        ##### 方法一：在请求头中使用API令牌（推荐）

        - **请求头**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: 点击页面右上角的`Authorize`按钮，直接输入API令牌，不需要`Bearer`关键字。


        ##### 方法二：在Cookie中使用API令牌（不推荐，仅在无法使用方法一时使用）

        - **Cookie**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `Authorization=Bearer your_token`


        #### 获取API令牌:

        1. 在TikHub网站注册并登录账户。

        2. 进入用户中心，点击API令牌菜单，创建API令牌。

        3. 复制并在请求头中使用API令牌。

        4. 保密您的API令牌，仅在请求头中使用。
      scheme: bearer
servers:
  - url: https://api.tikhub.io
    description: Production Environment
security: []

```
# 获取笔记信息 V4/Get note info V4

## OpenAPI Specification

```yaml
openapi: 3.0.1
info:
  title: ''
  description: ''
  version: 1.0.0
paths:
  /api/v1/xiaohongshu/web/get_note_info_v4:
    get:
      summary: 获取笔记信息 V4/Get note info V4
      deprecated: false
      description: >-
        # [中文]

        ### 用途:

        - 获取笔记信息V4

        ### 参数:

        - note_id: 笔记ID，可以从小红书的分享链接中获取

        - share_text: 小红书分享链接（支持APP和Web端分享链接）

        - 优先使用`note_id`，如果没有则使用`share_text`，两个参数二选一，如都携带则以`note_id`为准。

        ### 返回:

        - 笔记信息


        # [English]

        ### Purpose:

        - Get note info V4

        ### Parameters:

        - note_id: Note ID, can be obtained from the sharing link of Xiaohongshu
        website.

        - share_text: Xiaohongshu sharing link (support APP and Web sharing
        link)

        - Prefer to use `note_id`, if not, use `share_text`, one of the two
        parameters is required, if both are carried, `note_id` shall prevail.

        ### Return:

        - Note info


        # [示例/Example]

        note_id="665f95200000000006005624"
      operationId: get_note_info_v4_api_v1_xiaohongshu_web_get_note_info_v4_get
      tags:
        - Xiaohongshu-Web-API
        - Xiaohongshu-Web-API
      parameters:
        - name: note_id
          in: query
          description: 笔记ID/Note ID
          required: false
          example: 665f95200000000006005624
          schema:
            type: string
            description: 笔记ID/Note ID
            default: ''
            title: Note Id
        - name: share_text
          in: query
          description: 分享链接/Share link
          required: false
          example: https://xhslink.com/a/EZ4M9TwMA6c3
          schema:
            type: string
            description: 分享链接/Share link
            default: ''
            title: Share Text
      responses:
        '200':
          description: Successful Response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ResponseModel'
          headers: {}
          x-apifox-name: OK
        '422':
          description: Validation Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/HTTPValidationError'
          headers: {}
          x-apifox-name: Parameter Error
      security:
        - HTTPBearer: []
          x-apifox:
            schemeGroups:
              - id: Vqxbw9mhhohOnUtt6es8m
                schemeIds:
                  - HTTPBearer
            required: true
            use:
              id: Vqxbw9mhhohOnUtt6es8m
            scopes:
              Vqxbw9mhhohOnUtt6es8m:
                HTTPBearer: []
      x-apifox-folder: Xiaohongshu-Web-API
      x-apifox-status: released
      x-run-in-apifox: https://app.apifox.com/web/project/4705614/apis/api-255542734-run
components:
  schemas:
    ResponseModel:
      properties:
        code:
          type: integer
          title: Code
          description: HTTP status code | HTTP状态码
          default: 200
        request_id:
          anyOf:
            - type: string
            - type: 'null'
          title: Request Id
          description: Unique request identifier | 唯一请求标识符
        message:
          type: string
          title: Message
          description: Response message (EN-US) | 响应消息 (English)
          default: Request successful. This request will incur a charge.
        message_zh:
          type: string
          title: Message Zh
          description: Response message (ZH-CN) | 响应消息 (中文)
          default: 请求成功，本次请求将被计费。
        support:
          type: string
          title: Support
          description: Support message | 支持消息
          default: 'Discord: https://discord.gg/aMEAS8Xsvz'
        time:
          type: string
          title: Time
          description: The time the response was generated | 生成响应的时间
        time_stamp:
          type: integer
          title: Time Stamp
          description: The timestamp the response was generated | 生成响应的时间戳
        time_zone:
          type: string
          title: Time Zone
          description: The timezone of the response time | 响应时间的时区
          default: America/Los_Angeles
        docs:
          anyOf:
            - type: string
            - type: 'null'
          title: Docs
          description: >-
            Link to the API Swagger documentation for this endpoint | 此端点的 API
            Swagger 文档链接
        cache_message:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message
          description: Cache message (EN-US) | 缓存消息 (English)
          default: >-
            This request will be cached. You can access the cached result
            directly using the URL below, valid for 24 hours. Accessing the
            cache will not incur additional charges.
        cache_message_zh:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Message Zh
          description: Cache message (ZH-CN) | 缓存消息 (中文)
          default: 本次请求将被缓存，你可以使用下面的 URL 直接访问缓存结果，有效期为 24 小时，访问缓存不会产生额外费用。
        cache_url:
          anyOf:
            - type: string
            - type: 'null'
          title: Cache Url
          description: The URL to access the cached result | 访问缓存结果的 URL
        router:
          type: string
          title: Router
          description: The endpoint that generated this response | 生成此响应的端点
          default: ''
        params:
          type: string
        data:
          anyOf:
            - type: string
            - type: 'null'
          title: Data
          description: The response data | 响应数据
      type: object
      title: ResponseModel
      x-apifox-orders:
        - code
        - request_id
        - message
        - message_zh
        - support
        - time
        - time_stamp
        - time_zone
        - docs
        - cache_message
        - cache_message_zh
        - cache_url
        - router
        - params
        - data
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    HTTPValidationError:
      properties:
        detail:
          items:
            $ref: '#/components/schemas/ValidationError'
          type: array
          title: Detail
      type: object
      title: HTTPValidationError
      x-apifox-orders:
        - detail
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
    ValidationError:
      properties:
        loc:
          items:
            anyOf:
              - type: string
              - type: integer
          type: array
          title: Location
        msg:
          type: string
          title: Message
        type:
          type: string
          title: Error Type
      type: object
      required:
        - loc
        - msg
        - type
      title: ValidationError
      x-apifox-orders:
        - loc
        - msg
        - type
      x-apifox-ignore-properties: []
      x-apifox-folder: ''
  securitySchemes:
    Bearer Token:
      type: bearer
      scheme: bearer
    HTTPBearer:
      type: bearer
      description: >
        ----

        #### API Token Introduction:

        ##### Method 1: Use API Token in the Request Header (Recommended)

        - **Header**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: Click on the `Authorize` button in the upper right
        corner of the page to enter the API token directly without the `Bearer`
        keyword.


        ##### Method 2: Use API Token in the Cookie (Not Recommended, Use Only
        When Method 1 is Unavailable)

        - **Cookie**: `Authorization`

        - **Format**: `Bearer {token}`

        - **Example**: `Authorization=Bearer your_token`


        #### Get API Token:

        1. Register and log in to your account on the TikHub website.

        2. Go to the user center, click on the API token menu, and create an API
        token.

        3. Copy and use the API token in the request header.

        4. Keep your API token confidential and use it only in the request
        header.


        ----


        #### API令牌简介:

        ##### 方法一：在请求头中使用API令牌（推荐）

        - **请求头**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `{"Authorization": "Bearer your_token"}`

        - **Swagger UI**: 点击页面右上角的`Authorize`按钮，直接输入API令牌，不需要`Bearer`关键字。


        ##### 方法二：在Cookie中使用API令牌（不推荐，仅在无法使用方法一时使用）

        - **Cookie**: `Authorization`

        - **格式**: `Bearer {token}`

        - **示例**: `Authorization=Bearer your_token`


        #### 获取API令牌:

        1. 在TikHub网站注册并登录账户。

        2. 进入用户中心，点击API令牌菜单，创建API令牌。

        3. 复制并在请求头中使用API令牌。

        4. 保密您的API令牌，仅在请求头中使用。
      scheme: bearer
servers:
  - url: https://api.tikhub.io
    description: Production Environment
security: []

```

