"""Read-only setup guidance shared by the console and CLI.

Reviewing a step is not a configuration save, agreement, activation or delivery proof.
The existing editors remain the only writers.
"""


def dense_pages_guide(routes):
    def step(key, title, sentence, address=None):
        return dict(key=key, title=title, sentence=sentence, address=address)

    return {
        'routes': routes,
        'cancel': 'Leaving setup does not undo settings already saved in direct settings. '
                  'Only review progress is kept in this browser tab; no document or agreement is stored here.',
        'steps': [
            step('explain', 'Understand dense pages',
                 'Dense pages can put several short pages on one longer fax page. The original document is kept. '
                 'The capability status and route states below describe this installation now, not a setup promise.'),
            step('prerequisites', 'Check what it needs',
                 'Choose a sending cloud route. You need settings:write to change its page setting. '
                 'Your provider must carry long pages unchanged; check its documented limits or obtain confirmation '
                 'from the provider. Check the recipient machine on its recipient page: unknown length stays at A4. '
                 'A recipient can refuse packing there. Any required recipient agreement must come from the '
                 'recipient, never from finishing this guide.', 'recipients/list'),
            step('edit', 'Open the route editor',
                 'In Providers & accounts, open the cloud provider and its Long pages control. '
                 'Its scope is the named provider route, not one recipient. A recipient choice of Never still '
                 'prevents packing. Keep the default off until the provider is checked. Direct settings remain '
                 'available throughout this guide.', 'delivery/connections'),
            step('review', 'Review the effect before changing it',
                 'Review all recipients using that route, their learned page limits and their packing choices. '
                 'If packing is ineligible, Faxbot keeps ordinary pages. This guide changes no routing or fallback. '
                 'Already prepared and queued attempts retain their captured identity, provider and document layout. '
                 'Fewer pages may affect per-page billing; shorter calls may affect per-minute billing. '
                 'Allowances, billing increments and unknown prices can leave money unchanged or unknown.',
                 'savings/prices'),
            step('save', 'Save only in direct settings',
                 'The Long pages switch saves immediately for that route; it has no later Apply step. '
                 'Each recipient page has its own Save for this number boundary. Changes affect later document '
                 'preparation, not documents already prepared. This guide has no editable settings or pending '
                 'activation of its own. Unsaved edits in another editor are not recovered by this guide.',
                 'delivery/connections'),
            step('verify', 'Understand what was checked',
                 'Reading saved settings does not prove that a provider preserves long pages or that a recipient '
                 'can receive them. This guide performs only local reads. Any test fax must use the separate '
                 'Send a fax action with a chosen document and recipient; no fax, email or remote account change '
                 'is triggered by Next, leaving or finishing setup.'),
            step('status', 'Return to status and results',
                 'Return to the capability status and refresh it after saving elsewhere. Its results count actual '
                 'use, not reviewed steps. With no eligible delivery yet there is no measured result. '
                 'Inspect Sent for an attempt and Savings results for the separate page, time and cost figures.',
                 'savings/results'),
        ],
    }
