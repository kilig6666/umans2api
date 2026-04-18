# umans2api 第一版多账号 / 保活 / 轮询 / 面板设计

日期：2026-04-17

## 1. 范围

本设计仅覆盖第一版目标：

- 多账号管理
- 登录态保活
- 请求轮询调度
- Web 管理面板

本版明确不做：

- 自动注册
- 自动补号
- 邮箱验证码链路

## 2. 背景

当前项目只有一个 `config.json` 和一个 `umasn2api.py`。

现状问题：

1. 仅支持单账号全局 cookie
2. 无数据库，无法持久化账号状态
3. 无保活线程，session 过期只能手工修复
4. 无账号池与轮询，所有请求都走同一个账号
5. 无管理面板，无法查看保活、错误和请求命中情况

## 3. 设计目标

第一版要把项目升级成“最小可运行系统”：

1. 支持保存多个 Umans 账号
2. 能定时检查每个账号 session 是否仍然有效
3. 能在 session 即将到期时自动做续期
4. `/v1/messages` 与 `/v1/chat/completions` 从账号池轮询选号
5. 某个账号失败时自动跳过并切下一个
6. 面板可查看账号、状态、轮询结果和请求日志
7. 保持现有 Anthropic / OpenAI 协议兼容

## 4. 官网保活结论

已验证到的上游行为：

1. `GET /api/auth/session`
   - 可返回当前登录态、plan、expires 等信息
   - 更像检测接口
2. `GET /`
   - 页面访问会带来 session cookie 刷新
3. `POST /api/chat`
   - 真实聊天请求也会带来 session cookie 刷新

因此第一版保活策略分两层：

- L1：`/api/auth/session` 作为轻量检测
- L2：`GET /` 作为优先续期手段，必要时再回退到轻量聊天探活

## 5. 总体架构

第一版拆成五层：

1. 启动层
2. 存储与配置层
3. 账号池与调度层
4. 上游访问与协议适配层
5. 管理面板层

建议目录：

```text
umans2api/
  umasn2api.py
  config.json
  docs/plans/
  templates/
  umans2api/
    __init__.py
    app.py
    config_store.py
    db.py
    models.py
    account_manager.py
    keepalive.py
    dispatch.py
    upstream_client.py
    protocol_anthropic.py
    protocol_openai.py
    admin_routes.py
    proxy_routes.py
```

## 6. 存储设计

引入 SQLite。

### 6.1 配置表

保存：

- 管理面板口令
- 代理 API Key
- 保活间隔
- 续期阈值
- 失败阈值
- 最大并发

### 6.2 账号表

建议字段：

- `id`
- `name`
- `email`
- `enabled`
- `cookies_json`
- `plan`
- `allowed_model_prefix`
- `last_session_expires_at`
- `last_keepalive_at`
- `last_chat_ok_at`
- `failures`
- `inflight_count`
- `cooldown_until`
- `last_error`
- `created_at`
- `updated_at`

### 6.3 请求日志表

保存：

- 请求时间
- 请求路径
- 请求模型
- 命中账号
- 成功/失败
- 状态码
- 耗时
- 错误摘要

## 7. 多账号配置迁移

兼容旧配置：

1. 首次启动读取 `config.json`
2. 若数据库里没有账号，则把当前 `cookies` 迁移成第一个账号
3. 若已有数据库账号，则以数据库为准
4. `config.json` 仍保留 host、port、初始 api_key 作为引导配置

## 8. 账号状态机

定义四个主状态：

- `healthy`
- `expiring`
- `cooling`
- `disabled`

转换规则：

- session 正常且聊天成功 -> `healthy`
- session 接近到期 -> `expiring`
- 连续失败但未彻底失效 -> `cooling`
- 明确失效（401 / 302 跳登录 / 页面变未登录）-> `disabled`

## 9. 保活设计

### 9.1 L1 检测

周期调用 `GET /api/auth/session`：

- 记录 `expires`
- 判断 session 是否有效
- 更新 plan 信息

### 9.2 L2 续期

当距离过期阈值较近时：

1. 优先 `GET /`
2. 若验证不稳定，再按需回退到轻量聊天请求

### 9.3 调度频率

建议：

- 常规检测：10~15 分钟一次
- 距离过期不足 20 分钟：触发续期
- 正常聊天成功也视为一次天然保活

## 10. 轮询调度设计

### 10.1 候选条件

可被选中的账号必须满足：

- `enabled = 1`
- 非 `disabled`
- 非冷却中
- session 有效
- 支持当前模型前缀
- `inflight_count` 未超上限

### 10.2 调度算法

第一版采用：

- Round-robin
- 叠加 inflight 限制
- 叠加失败惩罚
- 叠加 cooldown 跳过

### 10.3 请求流程

1. 代理接口收到请求
2. 调度器 `reserve_next()`
3. 记录 lease
4. 发起上游请求
5. 成功 -> `mark_ok()`
6. 失败 -> `mark_fail()`
7. 达阈值进入 cooling / disabled
8. 当前请求允许一次自动切号重试

## 11. 面板设计

第一版面板使用单页 HTML。

Tab 规划：

1. 聊天调试
2. 账号管理
3. 保活监控
4. 轮询监控
5. 配置管理
6. 请求日志

### 11.1 账号管理

支持：

- 新增账号
- 编辑账号
- 启用/禁用
- 手工测试 session
- 手工触发 keepalive

### 11.2 保活监控

展示：

- 当前状态
- session expires
- 最近一次检测
- 最近一次续期
- 最近错误

### 11.3 轮询监控

展示：

- 当前轮询位置
- 各账号 inflight
- 最近请求命中账号

## 12. API 设计

### 12.1 管理 API

- `GET /api/accounts`
- `POST /api/accounts`
- `PUT /api/accounts/<id>`
- `DELETE /api/accounts/<id>`
- `POST /api/accounts/<id>/test-session`
- `POST /api/accounts/<id>/keepalive`
- `POST /api/accounts/<id>/enable`
- `POST /api/accounts/<id>/disable`
- `GET /api/stats`
- `GET /api/logs`
- `GET /api/config`
- `PUT /api/config`

### 12.2 代理 API

继续保留：

- `GET /v1/models`
- `POST /v1/messages`
- `POST /v1/chat/completions`

## 13. 实施阶段

### Phase 0：基线固定

- 保留当前单账号可用链路
- 固定兼容迁移策略

### Phase 1：存储与账号池

- 引入 SQLite
- 完成账号表 / 配置表 / 日志表
- 完成旧配置迁移

### Phase 2：保活

- `/api/auth/session` 检测
- `GET /` 续期
- 状态机与后台线程

### Phase 3：轮询调度

- lease
- reserve / release
- 失败切换
- 请求日志

### Phase 4：管理面板

- 基础管理 API
- 单页面板

### Phase 5：回归验证

- 单账号兼容
- 多账号轮询
- 保活触发
- 流式协议兼容

## 14. 风险

1. 官网保活行为部分依赖真实运行观察
2. 不同账号可能有不同 plan 与模型权限
3. 单文件项目首次拆分改动面较大
4. 当前上游 SSE/工具协议若变化，需要适配层兜底

## 15. 验收标准

1. 面板可看到多个账号
2. 能显示每个账号 plan / expires / 健康状态
3. 后台能定时检测 session
4. 快过期账号可自动续期
5. `/v1/messages` 可轮询不同账号
6. 某个账号失效时自动切下一个
7. 请求日志可看到命中账号
8. Anthropic / OpenAI 兼容接口不被破坏
