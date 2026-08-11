SELECT json_build_object(

    'tables', (
      SELECT json_agg(t ORDER BY t.table_name) FROM (
        SELECT
          c.table_name,
          json_agg(
            json_build_object(
              'column',   c.column_name,
              'type',     c.data_type,
              'nullable', c.is_nullable = 'YES',
              'default',  c.column_default
            ) ORDER BY c.ordinal_position
          ) AS columns
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
        GROUP BY c.table_name
      ) t
    ),

    'rls', (
      SELECT json_agg(r ORDER BY r.table, r.policy) FROM (
        SELECT
          tablename  AS table,
          policyname AS policy,
          cmd        AS command,
          roles,
          qual       AS using_expr,
          with_check
        FROM pg_policies
        WHERE schemaname = 'public'
      ) r
    ),

    'rls_status', (
      SELECT json_agg(s ORDER BY s.table) FROM (
        SELECT
          c.relname             AS table,
          c.relrowsecurity      AS rls_enabled,
          c.relforcerowsecurity AS rls_forced
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
      ) s
    ),

    'table_privileges', (
      SELECT json_agg(p ORDER BY p.table, p.grantee, p.privilege) FROM (
        SELECT
          table_name     AS table,
          grantee,
          privilege_type AS privilege,
          is_grantable = 'YES' AS is_grantable
        FROM information_schema.table_privileges
        WHERE table_schema = 'public'
          AND grantee IN ('anon', 'authenticated', 'service_role', 'public')
      ) p
    ),

    'data_api_grants_summary', (
      SELECT json_agg(g ORDER BY g.table) FROM (
        SELECT
          c.relname AS table,
          COALESCE(bool_or(tp.grantee = 'anon'), false) AS exposed_to_anon,
          COALESCE(bool_or(tp.grantee = 'authenticated'), false) AS exposed_to_authenticated,
          COALESCE(
            jsonb_agg(
              DISTINCT jsonb_build_object(
                'grantee', tp.grantee,
                'privilege', tp.privilege_type
              )
            ) FILTER (WHERE tp.grantee IS NOT NULL),
            '[]'::jsonb
          ) AS grants
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN information_schema.table_privileges tp
          ON tp.table_schema = n.nspname
          AND tp.table_name = c.relname
          AND tp.grantee IN ('anon', 'authenticated')
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'v', 'm')
        GROUP BY c.relname
      ) g
    ),

    'functions', (
      SELECT json_agg(f ORDER BY f.name) FROM (
        SELECT
          p.proname                        AS name,
          pg_get_function_arguments(p.oid) AS args,
          t.typname                        AS returns,
          p.prosecdef                      AS security_definer,
          pg_get_functiondef(p.oid)        AS definition
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_type t      ON t.oid = p.prorettype
        WHERE n.nspname = 'public' AND p.prokind = 'f'
      ) f
    ),

    'foreign_keys', (
      SELECT json_agg(fk ORDER BY fk.table, fk.column) FROM (
        SELECT
          tc.table_name   AS table,
          kcu.column_name AS column,
          ccu.table_name  AS references_table,
          ccu.column_name AS references_column,
          rc.delete_rule
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
          AND tc.table_schema   = kcu.table_schema
        JOIN information_schema.referential_constraints rc
          ON tc.constraint_name = rc.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON rc.unique_constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema    = 'public'
      ) fk
    ),

    'indexes', (
      SELECT json_agg(i ORDER BY i.table, i.index) FROM (
        SELECT
          tablename AS table,
          indexname AS index,
          indexdef  AS definition
        FROM pg_indexes
        WHERE schemaname = 'public'
      ) i
    ),

    'enums', (
      SELECT json_agg(e ORDER BY e.name) FROM (
        SELECT
          t.typname                                          AS name,
          array_agg(en.enumlabel ORDER BY en.enumsortorder) AS values
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        LEFT JOIN pg_enum en ON en.enumtypid = t.oid
        WHERE t.typtype = 'e' AND n.nspname = 'public'
        GROUP BY t.typname
      ) e
    ),

    'triggers', (
      SELECT json_agg(tr ORDER BY tr.table, tr.trigger) FROM (
        SELECT
          event_object_table AS table,
          trigger_name       AS trigger,
          event_manipulation AS event,
          action_timing      AS timing,
          action_statement   AS statement
        FROM information_schema.triggers
        WHERE trigger_schema = 'public'
      ) tr
    )

  ) AS project_schema;