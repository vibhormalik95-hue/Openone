\set ON_ERROR_STOP on
\getenv app_password HVM_APP_PASSWORD
\getenv worker_password HVM_WORKER_PASSWORD
\getenv billing_password HVM_BILLING_PASSWORD
SELECT format('ALTER ROLE hivemind_app PASSWORD %L', :'app_password') \gexec
SELECT format('ALTER ROLE hivemind_worker PASSWORD %L', :'worker_password') \gexec
SELECT format('ALTER ROLE hivemind_billing PASSWORD %L', :'billing_password') \gexec
