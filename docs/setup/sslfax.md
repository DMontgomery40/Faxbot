# Faster pages with SSL Fax

Faxbot can send pages faster when the recipient's fax machine supports SSL Fax. This is separate from T.38. Faxbot can use a recipient's SSL Fax connection without opening a port on your router. To let other fax machines connect to Faxbot, publish a listener port.

## Let other fax machines connect

1. In **Providers → Carrier trunk**, leave **Send pages faster when the other fax machine can (recommended)** on. Set **Router port for faster faxes** to the port you want to use; the default is `10443`.
2. Start Faxbot with the SSL Fax Compose file:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.sslfax.yml up -d
   ```

3. Forward that TCP port on your router to the Faxbot computer. If you changed the port in Faxbot, use the same port in the router rule.

Faxbot cannot check from inside your network whether the router forward works. The listener port is only needed when other fax machines must connect to Faxbot; sending pages to a machine that offers SSL Fax does not require this Compose file or a port forward.
