/*
 * Removes the u_hardware_requester role, its access rules and every grant of it.
 * Run as an admin elevated to security_admin, in System Definition > Scripts - Background.
 */
(function () {
  var ROLE = 'u_hardware_requester';
  var MARK = '[hardware-agent]';
  var acl = new GlideRecord('sys_security_acl');
  acl.addQuery('description', 'STARTSWITH', MARK);
  acl.query();
  var n = 0;
  while (acl.next()) {
    var link = new GlideRecord('sys_security_acl_role');
    link.addQuery('sys_security_acl', acl.getUniqueValue());
    link.deleteMultiple();
    acl.deleteRecord();
    n++;
  }
  var role = new GlideRecord('sys_user_role');
  if (role.get('name', ROLE)) {
    var grants = new GlideRecord('sys_user_has_role');
    grants.addQuery('role', role.getUniqueValue());
    grants.deleteMultiple();
    role.deleteRecord();
  }
  gs.print('Removed ' + n + ' access rules and role ' + ROLE + '.');
})();
