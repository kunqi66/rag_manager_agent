# specs/ — 功能 spec 产物目录

本目录存放「spec 驱动开发」的产物：每个前端页面 / 后端模块一份 spec 文件。

## 命名规则

`{P1|P2|P3}-{NN}-{fe|be}-{名称}.md`

| 例子 | 含义 |
|---|---|
| `P1-01-be-auth.md` | P1 第 1 个后端模块：认证 |
| `P1-02-fe-login.md` | P1 第 2 个前端页面：登录页 |

## 生成方式

通过 `.claude/skills/dev-spec` 生成，模板见
`vibecoding_doc/spec-templates/`，流程与门禁见 `vibecoding_doc/开发过程总纲.md`。

> 本目录内容随开发产出，纳入版本管理（与 PRD 一样是设计资产）。
