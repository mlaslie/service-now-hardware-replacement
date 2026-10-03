/*
 * Hardware Replacement agent: the u_hardware_requester role (instead of itil).
 *
 * Gives requesters exactly what the agent does for them, on their own hardware tickets, and
 * nothing else. Run once as an admin in ServiceNow:
 *   1. Elevate to security_admin (user menu > Elevate role), because only an elevated session can create ACLs.
 *   2. System Definition > Scripts - Background: paste this script, Run script.
 * Safe to run again: it updates what it created. Remove everything with remove_hardware_requester_role.js.
 * Ticket category below must match servicenow.ticket_category in config/organization.yaml.
 * Then grant the role:  uv run python scripts/sn_custom_role.py grant <user_name>
 * and measure it:       uv run python scripts/sn_doctor.py --matrix --persona none --persona u_hardware_requester --persona itil
 */
(function () {
  var ROLE = 'u_hardware_requester';
  var CATEGORY = 'hardware';
  var MARK = '[hardware-agent]';

  // Conditions, evaluated per record. "Mine": I reported it. "Following": I'm on its watch list.
  var HW = "current.category == '" + CATEGORY + "'";
  var MINE = "current.caller_id == gs.getUserID()";
  var FOLLOWING = "String(current.watch_list).indexOf(gs.getUserID()) > -1";
  var OPEN = "current.active == true";
  // Open tickets on a device record: equipment others may have reported ("already reported").
  var EQUIPMENT = "(" + OPEN + " && !current.cmdb_ci.nil())";

  var RULES = [
    // [table or table.field, operation, script (empty = role only), what it allows]
    ['incident', 'write', HW + ' && (' + MINE + ' || ' + FOLLOWING + ' || ' + EQUIPMENT + ')',
     'update own or followed hardware tickets, and follow open equipment tickets'],
    ['incident.description', 'write', HW + ' && ' + MINE, 'ticket details and ship-to on own tickets'],
    ['incident.urgency', 'write', HW + ' && ' + MINE, 'urgency on own tickets'],
    ['incident.impact', 'write', HW + ' && ' + MINE, 'impact on own tickets'],
    ['incident.state', 'write', HW + ' && ' + MINE, 'reopen, put on hold, cancel own tickets'],
    ['incident.close_code', 'write', HW + ' && ' + MINE, 'cancel own tickets'],
    ['incident.close_notes', 'write', HW + ' && ' + MINE, 'cancel own tickets'],
    ['incident.hold_reason', 'write', HW + ' && ' + MINE, 'put own tickets on hold'],
    // Others' tickets: only open equipment tickets, and the business rule below only lets a
    // requester add or remove themselves (never anyone else).
    ['incident.watch_list', 'write', HW + ' && (' + MINE + ' || ' + EQUIPMENT + ')', 'follow open equipment tickets'],
    ['incident.comments', 'write', HW + ' && (' + MINE + ' || ' + FOLLOWING + ' || ' + EQUIPMENT + ')',
     'notes on own, followed or open equipment tickets'],
    ['incident', 'read', HW + ' && (' + MINE + ' || ' + FOLLOWING + ' || (' + OPEN + ' && !current.cmdb_ci.nil()))',
     'see own and followed tickets, and open tickets on equipment ("already reported")'],
    ['cmdb_ci', 'read', '', 'device records, to link tickets to equipment'],
    ['sys_user_grmember', 'read', "current.user == gs.getUserID()", 'own group memberships']
  ];

  var role = new GlideRecord('sys_user_role');
  if (!role.get('name', ROLE)) {
    role.initialize();
    role.name = ROLE;
    role.description = MARK + ' Hardware Replacement agent: report and manage own hardware tickets, follow open ones. ' +
      'Grants nothing outside hardware incidents, device records and own group memberships.';
    role.insert();
    gs.print('created role ' + ROLE);
  }
  var roleId = role.getUniqueValue();

  for (var i = 0; i < RULES.length; i++) {
    var name = RULES[i][0], op = RULES[i][1], script = RULES[i][2], what = RULES[i][3];
    // Our rules are found through their link to the role: ServiceNow rewrites a new rule's description.
    var acl = new GlideRecord('sys_security_acl');
    var linked = new GlideRecord('sys_security_acl_role');
    linked.addQuery('sys_user_role', roleId);
    linked.addQuery('sys_security_acl.name', name);
    linked.addQuery('sys_security_acl.operation', op);
    linked.query();
    var isNew = !(linked.next() && acl.get(linked.getValue('sys_security_acl')));
    if (isNew) {
      acl.initialize();
      acl.name = name;
      acl.operation = op;
    }
    acl.type = 'record';
    acl.active = true;
    acl.admin_overrides = true;
    acl.advanced = script ? true : false;
    acl.script = script ? 'answer = (' + script + ');' : '';
    acl.description = MARK + ' ' + ROLE + ': ' + what;
    var aclId = isNew ? acl.insert() : (acl.update() && acl.getUniqueValue());
    if (!aclId) {
      gs.print('FAILED ' + name + ' ' + op + ': is this session elevated to security_admin?');
      continue;
    }
    var link = new GlideRecord('sys_security_acl_role');
    link.addQuery('sys_security_acl', aclId);
    link.addQuery('sys_user_role', roleId);
    link.query();
    if (!link.next()) {
      link.initialize();
      link.sys_security_acl = aclId;
      link.sys_user_role = roleId;
      link.insert();
    }
    gs.print((isNew ? 'created ' : 'updated ') + name + ' ' + op);
  }

  // An access rule can't see the new value, so a business rule checks watch list changes on
  // others' tickets: a requester (without itil) may only add or remove themselves.
  var BR = 'u_hardware_requester: follow only';
  var guard = [
    '(function executeRule(current, previous) {',
    "  if (gs.hasRole('itil') || !gs.hasRole('" + ROLE + "')) return;",
    '  var me = gs.getUserID();',
    '  if (current.getValue("caller_id") == me) return;',
    '  var split = function (v) { return String(v || "").split(",").filter(function (x) { return x; }); };',
    '  var before = split(previous.getValue("watch_list")), after = split(current.getValue("watch_list"));',
    '  var changed = after.filter(function (x) { return before.indexOf(x) < 0; })',
    '    .concat(before.filter(function (x) { return after.indexOf(x) < 0; }));',
    '  if (changed.some(function (x) { return x != me; })) {',
    "    gs.addErrorMessage('You can follow or stop following this ticket, but not change who else follows it.');",
    '    current.setAbortAction(true);',
    '  }',
    '})(current, previous);'
  ].join('\n');
  var br = new GlideRecord('sys_script');
  var brNew = !br.get('name', BR);
  if (brNew) br.initialize();
  br.name = BR;
  br.collection = 'incident';
  br.when = 'before';
  br.action_update = true;
  br.action_insert = false;
  br.advanced = true;
  br.active = true;
  br.order = 100;
  br.condition = "current.watch_list.changes() && current.category == '" + CATEGORY + "'";
  br.script = guard;
  br.description = MARK + ' ' + ROLE + ': requesters may only add or remove themselves on others\' tickets';
  gs.print((brNew ? (br.insert() ? 'created ' : 'FAILED ') : (br.update() ? 'updated ' : 'FAILED ')) +
           'business rule "' + BR + '"');
  gs.print('Done: role ' + ROLE + ' with ' + RULES.length + ' access rules and 1 business rule.');
})();
