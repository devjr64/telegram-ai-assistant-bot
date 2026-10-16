# Fix: Group Project Collaboration

## Problem
When a user creates a group project, other group members do NOT see it in their private chat because they are not added as collaborators.

## Root Cause
`_handle_create_project` calls `create_project()` directly instead of `create_group_project()` which automatically adds group members.

## Changes Made

### 1. `bot/tools.py` — `_handle_create_project`
When `_group_id` is present, use `create_group_project` instead of `create_project`.

### 2. `bot/projects.py` — `add_group_member`
When a new user joins the group, they are added as a collaborator to all existing group projects.

### 3. `bot/projects.py` — `list_group_projects_for_user`
New function that returns all group projects where the user is a member or owner.

### 4. `bot/tools.py` — `_list_visible_projects`
Now includes group projects in the user's visible list in private chat.

## Note for Future
Removing collaborators from group projects is only done from private chat with `/quitaracceso`. There is no need to add this functionality in the group.

## Verification
- @usuario_a creates project in group
- @usuario_b should see it in their private chat
- @usuario_b should be able to edit it
