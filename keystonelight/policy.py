# vim: tabstop=4 shiftwidth=4 softtabstop=4

# the catalog interfaces

import uuid

from keystonelight import utils


class Manager(object):
    def __init__(self, options):
        self.options = options
        self.driver = utils.import_object(options['policy_driver'],
                                          options=options)

    def can_haz(self, context, target, credentials):
        """Check whether the given creds can perform action on target."""
        return self.driver.can_haz(target, credentials)

    def add_rule(self, context, target, rule):
        pass






"""Brain format:

    target:
        nova:action:reboot_instance

    rule:
        roles:admin
        roles:sysadmin, tenant_id:%(tenant_id)s


    >>> can_haz(target='nova:action:reboot_instance',
                extra={'tenant_id': object.project_id},
                creds={'user_id': context.user.id,
                       'tenant_id': context.tenant.id,
                       'roles': ['role:%s' for x['id'] in context.roles]})


    results in background operations

    def _check_haz(target, extra, creds):
        rules = brain.get_rule('nova:action:reboot_instance')
        for rule in rules:
            if self.check_rule(rule, extra, creds):
                return True
        return False

    def _check_rule(rule, extra, creds):
        if type(rule) is type(tuple()) or type(rule) is type(list()):
            for x in rule:
                if not self._check_rule(x, extra, creds):
                    return False




"""

class
