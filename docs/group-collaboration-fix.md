# Fix: Group Project Collaboration

## Problema
Cuando un usuario crea un proyecto en grupo, los otros miembros del grupo NO lo ven en su chat privado porque no se les añade como colaboradores.

## Causa Raíz
`_handle_create_project` llama `create_project()` directamente en lugar de `create_group_project()` que añade automáticamente a los miembros del grupo.

## Cambios Realizados

### 1. `bot/tools.py` — `_handle_create_project`
Cuando `_group_id` esté presente, usa `create_group_project` en lugar de `create_project`.

### 2. `bot/projects.py` — `add_group_member`
Cuando un nuevo usuario se une al grupo, se le añade como colaborador a todos los proyectos existentes del grupo.

### 3. `bot/projects.py` — `list_group_projects_for_user`
Nueva función que retorna todos los proyectos de grupo donde el usuario es miembro o dueño.

### 4. `bot/tools.py` — `_list_visible_projects`
Ahora incluye proyectos de grupo en la lista visible del usuario en chat privado.

## Nota para Futuro
La eliminación de colaboradores de proyectos de grupo solo se hace desde chat privado con `/quitaracceso`. No hay necesidad de añadir esta funcionalidad en el grupo.

## Verificación
- @usuario_a crea proyecto en grupo
- @usuario_b debe verlo en su chat privado
- @usuario_b debe poder editarlo
