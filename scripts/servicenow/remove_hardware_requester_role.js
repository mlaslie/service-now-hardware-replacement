/*
 * Removes the u_hardware_requester role, its access rules, its business rule and every grant of it.
 * Run as an admin elevated to security_admin, in System Definition > Scripts - Background.
 * Reports anything it could not delete; the role itself is only removed once its rules are gone,
 * because a rule left without its role would apply to everyone.
 */
(function () {
  var ROLE = 'u_hardware_requester';
  var BR = 'u_hardware_requester: follow only';
  var removed = 0, failed = 0;
  var role = new GlideRecord('sys_user_role');
  if (!role.get('name', ROLE)) {
    gs.print('Role ' + ROLE + ' not found: nothing to remove.');
  } else {
    var roleId = role.getUniqueValue();
    // Every rule linked to the role (ServiceNow rewrites rule descriptions, so links are the reliable key).
    var ids = [];
    var link = new GlideRecord('sys_security_acl_role');
    link.addQuery('sys_user_role', roleId);
    link.query();
    while (link.next()) ids.push(link.getValue('sys_security_acl'));
    for (var i = 0; i < ids.length; i++) {
      var acl = new GlideRecord('sys_security_acl');
      if (!acl.get(ids[i])) continue;
      var name = acl.getValue('name') + ' ' + acl.getValue('operation');
      acl.deleteRecord();
      if (new GlideRecord('sys_security_acl').get(ids[i])) {
        failed++;
        gs.print('FAILED to delete rule ' + name + ': is this session elevated to security_admin?');
        continue;
      }
      var others = new GlideRecord('sys_security_acl_role');
      others.addQuery('sys_security_acl', ids[i]);
      others.deleteMultiple();
      removed++;
    }
    if (failed) {
      gs.print('Stopped: ' + failed + ' rule(s) remain, so the role was kept. Elevate to security_admin and run again.');
      return;
    }
    var tables = ['sys_user_has_role', 'sys_group_has_role'];
    for (var t = 0; t < tables.length; t++) {
      var grants = new GlideRecord(tables[t]);
      grants.addQuery('role', roleId);
      grants.deleteMultiple();
    }
    var contains = new GlideRecord('sys_user_role_contains');
    contains.addQuery('contains', roleId);
    contains.deleteMultiple();
    role.deleteRecord();
    gs.print(new GlideRecord('sys_user_role').get('name', ROLE) ? 'FAILED to delete role ' + ROLE
                                                                 : 'Deleted role ' + ROLE + ' and its grants.');
  }
  var br = new GlideRecord('sys_script');
  if (br.get('name', BR)) {
    br.deleteRecord();
    gs.print('Deleted business rule "' + BR + '".');
  }
  gs.print('Removed ' + removed + ' access rules.');
})();
