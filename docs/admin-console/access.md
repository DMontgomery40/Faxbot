# People and access

**Administration → People & access** shows who may use Faxbot and what they may do. See [Access Control](../security/access-control.md) for the permissions behind each page.

| Page | Address | What it holds |
| --- | --- | --- |
| Users | `#/admin/users` | People and connected systems; add a person and show the temporary password once. |
| Groups | `#/admin/groups` | Groups and their members. |
| Roles | `#/admin/roles` | Built-in roles (read only) and your own roles. |
| Who has access | `#/admin/who` | Who has which role on everything, on a mailbox, or on someone's own faxes. |
| Keys & phones | `#/admin/keys` | API keys and paired phones, the **Installation key** (read only, "Set in .env", never shown), and **Address phones use on your network**. See [API keys](api-keys.md). |
| Sessions | `#/admin/sessions` | Signed-in sessions; everyone can see and end their own. |

**My API keys** in your menu opens Keys & phones with only your own keys.
