/*
 * Removes the u_hardware_requester role, its access rules and every grant of it.
 * Run as an admin elevated to security_admin, in System Definition > Scripts - Background.
 */
(function () {
  var ROLE = 'u_hardware_requester';
  var n = 0;
  var role = new GlideRecord('sys_user_role');
  if (role.get('name', ROLE)) {
    // Every rule linked to the role (ServiceNow rewrites rule descriptions, so links are the reliable key).
    var link = new GlideRecord('sys_security_acl_role');
    link.addQuery('sys_user_role', role.getUniqueValue());
    link.query();
    while (link.next()) {
      var acl = new GlideRecord('sys_security_acl');
      if (acl.get(link.getValue('sys_security_acl'))) {
        var others = new GlideRecord('sys_security_acl_role');
        others.addQuery('sys_security_acl', acl.getUniqueValue());
        others.deleteMultiple();
        acl.deleteRecord();
        n++;
      }
    }
  }
  role = new GlideRecord('sys_user_role');
  if (role.get('name', ROLE)) {
    var grants = new GlideRecord('sys_user_has_role');
    grants.addQuery('role', role.getUniqueValue());
    grants.deleteMultiple();
    role.deleteRecord();
  }
  gs.print('Removed ' + n + ' access rules and role ' + ROLE + '.');
})();
