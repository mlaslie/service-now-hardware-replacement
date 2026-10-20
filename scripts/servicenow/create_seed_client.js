/*
 * Hardware Replacement agent: the "Hardware Seed Script" OAuth client, for a new or rebuilt instance.
 *
 * The admin tools (seed/sn_seed.py, scripts/sn_doctor.py, scripts/sn_profile.py, scripts/rebuild_instance.py)
 * sign in as an admin through this client. It is the only OAuth client that has to exist before those tools
 * can run; rebuild_instance.py creates the Gemini Enterprise client itself, through the API.
 *
 * Usually not pasted on its own: `uv run python scripts/rebuild_instance.py bootstrap` copies this script
 * together with create_hardware_requester_role.js, to run once in System Definition > Scripts - Background
 * (elevated to security_admin, for the role's access rules).
 *
 * Safe to run again: it reuses the client if it exists. The last line printed is
 *   SEED_CLIENT=<instance>|<client id>|<client secret>
 * which rebuild_instance.py asks for (hidden input) and stores in Secret Manager.
 */
(function () {
  var NAME = 'Hardware Seed Script';
  var REDIRECT = 'http://localhost:8765/callback';

  var c = new GlideRecord('oauth_entity');
  if (c.get('name', NAME)) {
    gs.print('seed client: exists, reused');
  } else {
    c.initialize();  // defaults generate the client id and secret
    c.setValue('name', NAME);
    c.setValue('type', 'client');
    c.setValue('inbound_grant_type', 'authz_code');
    c.setValue('redirect_url', REDIRECT);
    c.setValue('public_client', false);
    c.setValue('use_pkce', false);
    c.setValue('active', true);
    c.setValue('access_token_lifespan', 1800);
    c.setValue('refresh_token_lifespan', 8640000);
    c.setValue('send_client_credentials_as', 'request_body_parameter');
    c.setValue('scope_restriction_status', 'useraccount');
    c.insert();
    gs.print('seed client: created');
  }
  if (c.getValue('redirect_url') != REDIRECT || c.getValue('active') != '1' && c.getValue('active') != 'true') {
    c.setValue('redirect_url', REDIRECT);
    c.setValue('active', true);
    c.update();
  }

  // The useraccount scope, as the Inbound integrations form links it (newer releases only).
  var scope = new GlideRecord('sys_auth_scope');
  if (gs.tableExists('oauth_entity_auth_scope_mapping') && scope.get('name', 'useraccount')) {
    var map = new GlideRecord('oauth_entity_auth_scope_mapping');
    map.addQuery('oauth_entity', c.getUniqueValue());
    map.addQuery('auth_scope', scope.getUniqueValue());
    map.query();
    if (!map.hasNext()) {
      map.initialize();
      map.setValue('oauth_entity', c.getUniqueValue());
      map.setValue('auth_scope', scope.getUniqueValue());
      map.insert();
    }
  }

  var secret = c.client_secret.getDecryptedValue();
  if (!secret) {
    secret = GlideSecureRandomUtil.getSecureRandomString(32);
    c.client_secret.setDisplayValue(secret);
    c.update();
  }
  var instance = String(gs.getProperty('glide.servlet.uri')).replace(/\/+$/, '');
  gs.print('Copy the next line into rebuild_instance.py when it asks (it is a credential; don\'t share it):');
  gs.print('SEED_CLIENT=' + instance + '|' + c.getValue('client_id') + '|' + secret);
})();
