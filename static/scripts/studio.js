(() => {
    'use strict';
    const composer = document.querySelector('#studio-composer');
    if (!composer) return;
    const form = document.querySelector('#studio-post-form');
    const connection = document.querySelector('#studio-connection');
    const message = document.querySelector('#studio-post-message');
    const photoList = document.querySelector('#studio-photos');
    const newButton = document.querySelector('#studio-new');
    const loginButton = document.querySelector('#studio-login');
    const hiddenButton = document.querySelector('#studio-hidden');
    const hiddenDialog = document.querySelector('#studio-hidden-dialog');
    const hiddenList = document.querySelector('#studio-hidden-list');
    const authDialog = document.querySelector('#studio-auth-dialog');
    const authForm = document.querySelector('#studio-auth-form');
    const profileDialog = document.querySelector('#studio-profile-dialog');
    const profileForm = document.querySelector('#studio-profile-form');
    const addUserForm = document.querySelector('#studio-add-user-form');
    const toast = document.querySelector('#studio-toast');
    const rows = [...document.querySelectorAll('.moment-row[data-moment-id]')];
    const moments = new Map();
    let session;
    let editing = null;
    let retainedPhotos = [];
    let uploads = [];
    let dirty = false;
    let submitting = false;
    let toastTimer;
    let profileAvatarURL;

    function canManage(moment) {
        return Boolean(session?.authenticated && (session.user?.role === 'owner' || moment.author_id === session.user?.id));
    }

    function reloadWithNotice(text) {
        sessionStorage.setItem('moments-notice', text);
        location.assign('/');
    }

    function say(text) {
        toast.textContent = text;
        toast.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { toast.hidden = true; }, 6000);
    }

    async function request(path, options = {}) {
        const headers = new Headers(options.headers || {});
        if (options.method && options.method !== 'GET') headers.set('X-Moments-Token', session.token);
        if (options.body && !(options.body instanceof FormData)) {
            headers.set('Content-Type', 'application/json');
            options.body = JSON.stringify(options.body);
        }
        const response = await fetch(path, { ...options, headers });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Your change could not be saved');
        return result;
    }

    function localDate() {
        const parts = new Intl.DateTimeFormat('en-GB', {
            timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
        }).formatToParts(new Date());
        const value = Object.fromEntries(parts.map(part => [part.type, part.value]));
        return `${value.year}-${value.month}-${value.day}T${value.hour}:${value.minute}`;
    }

    function saveLocation() {
        const date = form.elements.date.value.split('T')[0].replaceAll('-', '/');
        document.querySelector('#studio-save-location').textContent = `Saved in Moments/${date}/`;
    }

    function photoURL(picture) {
        if (/^(https?:\/\/|\/\/|\/)/.test(picture)) return picture;
        const relative = picture.replace(/^pictures\//, '').split('/').map(encodeURIComponent).join('/');
        return `/api/moments/${editing.id}/pictures/${relative}`;
    }

    function renderPhotos() {
        photoList.replaceChildren();
        const pictures = [
            ...retainedPhotos.map(src => ({ src: photoURL(src), key: src, retained: true })),
            ...uploads.map(item => ({ src: item.url, key: item, retained: false }))
        ];
        for (const picture of pictures) {
            const tile = document.createElement('div');
            tile.className = 'studio-photo';
            const image = document.createElement('img');
            image.src = picture.src;
            image.alt = 'Attached photo';
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'studio-remove-photo';
            button.textContent = '×';
            button.setAttribute('aria-label', 'Remove photo from this moment');
            button.addEventListener('click', () => {
                if (picture.retained) retainedPhotos = retainedPhotos.filter(src => src !== picture.key);
                else {
                    URL.revokeObjectURL(picture.key.url);
                    uploads = uploads.filter(item => item !== picture.key);
                }
                dirty = true;
                renderPhotos();
            });
            tile.append(image, button);
            photoList.append(tile);
        }
    }

    function addPhotos(files) {
        for (const file of files) {
            if (!['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/avif'].includes(file.type)) {
                say('Choose JPEG, PNG, GIF, WebP, or AVIF photos.');
                continue;
            }
            if (file.size > 10 * 1024 * 1024) { say('Each photo must be 10 MB or smaller.'); continue; }
            if (retainedPhotos.length + uploads.length >= 20) { say('You can attach up to 20 photos.'); break; }
            uploads.push({ file, url: URL.createObjectURL(file) });
            dirty = true;
        }
        renderPhotos();
    }

    function openComposer(moment = null) {
        for (const item of uploads) URL.revokeObjectURL(item.url);
        uploads = [];
        retainedPhotos = moment ? [...moment.pictures] : [];
        editing = moment;
        form.reset();
        message.textContent = '';
        form.querySelector('details').open = false;
        if (hiddenDialog.open) hiddenDialog.close();
        document.querySelector('#studio-composer-title').textContent = moment ? (moment.hidden ? 'Edit hidden moment' : moment.draft ? 'Edit draft' : 'Edit moment') : 'New moment';
        document.querySelector('#studio-publish').textContent = moment && !moment.draft ? 'Save changes' : 'Post';
        if (moment) {
            for (const key of ['body', 'note', 'link', 'link_text', 'top']) form.elements[key].value = moment[key] || '';
            form.elements.tags.value = moment.tags.join(', ');
        }
        form.elements.date.value = moment ? moment.date.slice(0, 16) : localDate();
        saveLocation();
        renderPhotos();
        dirty = false;
        composer.showModal();
        form.elements.body.focus();
    }

    function closeComposer() {
        if (submitting) return;
        if (dirty && !window.confirm('Discard the unsaved changes?')) return;
        dirty = false;
        composer.close();
    }

    function socialForRow(row, social) {
        const like = row.querySelector('.studio-like');
        if (!like) return;
        like.setAttribute('aria-pressed', String(Boolean(social.liked)));
        like.firstElementChild.textContent = social.liked ? '♥' : '♡';
        const likeCount = Number(social.like_count ?? (social.liked ? 1 : 0));
        row.querySelector('.studio-like-label').textContent = (social.liked ? 'Liked' : 'Like') + (likeCount ? ` · ${likeCount}` : '');
        row.querySelector('.studio-comment-count').textContent = social.comments.length;
        const list = row.querySelector('.studio-comment-list');
        list.replaceChildren();
        for (const comment of social.comments) {
            const item = document.createElement('article');
            item.className = 'studio-comment';
            if (comment.parent_id) item.classList.add('studio-comment-reply');
            const heading = document.createElement('div');
            heading.className = 'studio-comment-heading';
            const name = document.createElement('strong');
            name.textContent = comment.author;
            if (comment.author_id) {
                const profileLink = document.createElement('a');
                profileLink.href = `/users/${encodeURIComponent(comment.author_id)}/`;
                profileLink.textContent = comment.author;
                name.replaceChildren(profileLink);
            }
            const when = document.createElement('time');
            when.dateTime = comment.date;
            when.textContent = new Date(comment.date).toLocaleString('en-GB', {
                timeZone: 'Asia/Shanghai', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
            }) + (comment.edited_at ? ' · edited' : '');
            heading.append(name, when);
            const content = document.createElement('p');
            content.textContent = comment.text;
            const actions = document.createElement('div');
            actions.className = 'studio-comment-actions';
            for (const action of ['Reply', 'Edit']) {
                if (!session.authenticated || (action === 'Edit' && session.user?.role !== 'owner' && comment.author_id !== session.user?.id)) continue;
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'studio-action';
                button.textContent = action;
                button.addEventListener('click', () => {
                    const commentForm = row.querySelector('.studio-comment-form');
                    commentForm.dataset.parentId = action === 'Reply' ? comment.id : '';
                    commentForm.dataset.editId = action === 'Edit' ? comment.id : '';
                    commentForm.elements.text.value = action === 'Edit' ? comment.text : '';
                    commentForm.elements.author.value = action === 'Edit' ? comment.author : session.author;
                    commentForm.elements.author.readOnly = true;
                    commentForm.querySelector('[type=submit]').textContent = action === 'Edit' ? 'Save comment' : 'Reply';
                    const status = commentForm.querySelector('.studio-reply-status');
                    status.replaceChildren(document.createTextNode(action === 'Edit' ? 'Editing your comment ' : `Replying to ${comment.author} `));
                    const cancel = document.createElement('button');
                    cancel.type = 'button';
                    cancel.className = 'studio-action';
                    cancel.textContent = 'Cancel';
                    cancel.addEventListener('click', () => resetCommentForm(commentForm));
                    status.append(cancel);
                    status.hidden = false;
                    commentForm.elements.text.focus();
                });
                actions.append(button);
            }
            item.append(heading, content, actions);
            list.append(item);
        }
        if (!social.comments.length) {
            const empty = document.createElement('p');
            empty.className = 'studio-muted';
            empty.textContent = 'No comments yet. Leave the first thought.';
            list.append(empty);
        }
    }

    function resetCommentForm(commentForm) {
        commentForm.reset();
        commentForm.dataset.parentId = '';
        commentForm.dataset.editId = '';
        commentForm.elements.author.readOnly = true;
        commentForm.elements.author.value = session.author;
        commentForm.querySelector('.studio-reply-status').hidden = true;
        commentForm.querySelector('[type=submit]').textContent = 'Comment';
    }

    function renderHidden() {
        const records = [...moments.values()].filter(moment => moment.hidden && canManage(moment));
        document.querySelector('#studio-hidden-count').textContent = records.length;
        hiddenList.replaceChildren();
        for (const moment of records) {
            const card = document.createElement('article');
            card.className = 'studio-hidden-card';
            card.dataset.momentId = moment.id;
            const when = document.createElement('p');
            when.className = 'studio-muted';
            when.textContent = new Date(moment.date).toLocaleString('en-GB', { timeZone: 'Asia/Shanghai' }) + (moment.draft ? ' · Draft' : '');
            const text = document.createElement('p');
            text.className = 'studio-hidden-body';
            text.textContent = moment.body.slice(0, 350) || (moment.pictures.length ? `${moment.pictures.length} photos` : moment.link_text || moment.link);
            const actions = document.createElement('div');
            actions.className = 'studio-actions';
            for (const label of ['Edit', 'Unhide', 'Delete']) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'studio-action' + (label === 'Delete' ? ' studio-action-danger' : '');
                button.textContent = label;
                button.addEventListener('click', async () => {
                    button.disabled = true;
                    try {
                        if (label === 'Edit') openComposer(await request(`/api/moments/${moment.id}`));
                        else if (label === 'Unhide') await changeVisibility(moment, false);
                        else await deleteMoment(moment);
                    } catch (error) { say(error.message); }
                    finally { button.disabled = false; }
                });
                actions.append(button);
            }
            card.append(when, text, actions);
            hiddenList.append(card);
        }
        if (!records.length) {
            const empty = document.createElement('p');
            empty.className = 'studio-help';
            empty.textContent = 'No hidden moments. Use Hide beside a post to keep it off your timeline.';
            hiddenList.append(empty);
        }
    }

    async function changeVisibility(moment, hidden) {
        const current = await request(`/api/moments/${moment.id}`);
        const result = await request(`/api/moments/${moment.id}/visibility`, {
            method: 'PUT', body: { hidden, revision: current.revision }
        });
        moments.set(moment.id, result.moment);
        if (hidden) rows.find(row => row.dataset.momentId === moment.id)?.remove();
        renderHidden();
        const notice = hidden ? 'Moment hidden. Find it under Hidden.' : 'Moment is visible again.';
        if (result.build_error) say(`${notice} The preview needs a correction: ${result.build_error}`);
        else reloadWithNotice(notice);
    }

    async function deleteMoment(moment) {
        if (!window.confirm('Move this moment to Trash? Its text, photos, comments, and history will be kept in your Moments/.trash folder.')) return;
        const current = await request(`/api/moments/${moment.id}`);
        const result = await request(`/api/moments/${moment.id}`, { method: 'DELETE', body: { revision: current.revision } });
        moments.delete(moment.id);
        rows.find(row => row.dataset.momentId === moment.id)?.remove();
        renderHidden();
        if (result.build_error) say(`Moment moved to Trash. The preview needs a correction: ${result.build_error}`);
        else reloadWithNotice('Moment moved to Trash. Its files are kept in Moments/.trash.');
    }

    function openLogin() {
        authForm.reset();
        document.querySelector('#studio-auth-message').textContent = '';
        const setup = session.setup_required;
        document.querySelector('#studio-auth-title').textContent = setup ? 'Set up your login' : 'Log in';
        document.querySelector('#studio-auth-submit').textContent = setup ? 'Create login' : 'Log in';
        document.querySelector('#studio-auth-description').textContent = setup ? 'Choose a password for your account to post, edit, and comment. Use at least 8 characters.' : 'Log in to post moments and join the conversation.';
        document.querySelector('#studio-auth-user-label').hidden = setup;
        authForm.elements.password.autocomplete = setup ? 'new-password' : 'current-password';
        const select = authForm.elements.user_id;
        select.replaceChildren();
        for (const user of session.users || []) {
            if (!setup && user.can_login === false) continue;
            const option = document.createElement('option');
            option.value = user.id;
            option.textContent = `${user.name} (@${user.id})`;
            select.append(option);
        }
        select.value = session.default_user_id || select.value;
        authDialog.showModal();
        authForm.elements.password.focus();
    }

    function openProfile() {
        const user = session.user;
        profileForm.reset();
        addUserForm.reset();
        profileForm.elements.name.value = user.name;
        profileForm.elements.bio.value = user.bio || '';
        document.querySelector('#studio-profile-login').textContent = `Login name: @${user.id}`;
        document.querySelector('#studio-profile-link').href = user.url || `/users/${encodeURIComponent(user.id)}/`;
        document.querySelector('#studio-profile-avatar').src = user.avatar || '/default-avatar.png';
        document.querySelector('#studio-profile-message').textContent = '';
        document.querySelector('#studio-add-user-message').textContent = '';
        const section = document.querySelector('#studio-add-user-section');
        section.hidden = user.role !== 'owner';
        section.open = false;
        profileDialog.showModal();
    }

    function accountUI(records) {
        const signedIn = Boolean(session.authenticated && session.user);
        const count = records.filter(moment => !moment.hidden).length;
        connection.textContent = 'Saved on this Mac · ' + count + (count === 1 ? ' moment' : ' moments');
        newButton.disabled = !signedIn;
        newButton.hidden = !signedIn;
        loginButton.hidden = signedIn;
        loginButton.textContent = session.setup_required ? 'Set up login' : 'Log in';
        hiddenButton.hidden = !signedIn;
        document.querySelector('#studio-profile').hidden = !signedIn;
        document.querySelector('#studio-logout').hidden = !signedIn;
        const account = document.querySelector('#studio-account');
        account.hidden = !signedIn;
        if (signedIn) {
            account.href = session.user.url || `/users/${encodeURIComponent(session.user.id)}/`;
            document.querySelector('#studio-account-name').textContent = session.user.name;
            document.querySelector('#studio-account-avatar').src = session.user.avatar || '/default-avatar.png';
        }
        renderHidden();
    }

    async function connect() {
        try {
            session = await request('/api/session');
            session.author = session.user?.name || session.author;
            const { moments: records } = await request('/api/moments');
            for (const moment of records) moments.set(moment.id, moment);
            accountUI(records);
            for (const row of rows) {
                const moment = moments.get(row.dataset.momentId);
                if (!moment) continue;
                socialForRow(row, moment.social);
                for (const control of row.querySelectorAll('.studio-edit, .studio-hide, .studio-delete')) control.hidden = !canManage(moment);
                const commentForm = row.querySelector('.studio-comment-form');
                if (commentForm) {
                    commentForm.hidden = !session.authenticated;
                    commentForm.elements.author.readOnly = true;
                    commentForm.elements.author.value = session.author;
                    if (!session.authenticated) {
                        const login = document.createElement('button');
                        login.type = 'button';
                        login.className = 'studio-action';
                        login.textContent = session.setup_required ? 'Set up login to comment' : 'Log in to comment';
                        login.addEventListener('click', openLogin);
                        row.querySelector('.studio-comments').append(login);
                    }
                }
                row.querySelector('.studio-edit')?.addEventListener('click', async () => {
                    try { openComposer(await request(`/api/moments/${moment.id}`)); } catch (error) { say(error.message); }
                });
                row.querySelector('.studio-hide')?.addEventListener('click', async event => {
                    const button = event.currentTarget;
                    button.disabled = true;
                    try { await changeVisibility(moment, true); } catch (error) { say(error.message); }
                    finally { button.disabled = false; }
                });
                row.querySelector('.studio-delete')?.addEventListener('click', async event => {
                    const button = event.currentTarget;
                    button.disabled = true;
                    try { await deleteMoment(moment); } catch (error) { say(error.message); }
                    finally { button.disabled = false; }
                });
                row.querySelector('.studio-like')?.addEventListener('click', async event => {
                    if (!session.authenticated) { openLogin(); return; }
                    const button = event.currentTarget;
                    button.disabled = true;
                    try {
                        moment.social = await request(`/api/moments/${moment.id}/like`, { method: 'POST', body: { liked: button.getAttribute('aria-pressed') !== 'true' } });
                        socialForRow(row, moment.social);
                    } catch (error) { say(error.message); }
                    finally { button.disabled = false; }
                });
                row.querySelector('.studio-comments-toggle')?.addEventListener('click', event => {
                    const panel = row.querySelector('.studio-comments');
                    panel.hidden = !panel.hidden;
                    event.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
                    if (!panel.hidden) panel.querySelector(session.authenticated ? 'textarea' : 'button')?.focus();
                });
                row.querySelector('.studio-comment-form')?.addEventListener('submit', async event => {
                    event.preventDefault();
                    const commentForm = event.currentTarget;
                    const button = commentForm.querySelector('[type=submit]');
                    const status = commentForm.querySelector('.studio-form-message');
                    button.disabled = true;
                    status.textContent = 'Saving…';
                    try {
                        const editId = commentForm.dataset.editId;
                        const path = `/api/moments/${moment.id}/comments` + (editId ? `/${editId}` : '');
                        moment.social = await request(path, { method: editId ? 'PUT' : 'POST', body: {
                            text: commentForm.elements.text.value, author: commentForm.elements.author.value,
                            parent_id: commentForm.dataset.parentId || null
                        } });
                        socialForRow(row, moment.social);
                        resetCommentForm(commentForm);
                        status.textContent = 'Saved';
                    } catch (error) { status.textContent = error.message; }
                    finally { button.disabled = false; }
                });
            }
            const notice = sessionStorage.getItem('moments-notice');
            if (notice) { sessionStorage.removeItem('moments-notice'); say(notice); }
        } catch (error) {
            connection.textContent = 'Start the Moments app to post, edit, and comment.';
            for (const row of rows) for (const button of row.querySelectorAll('.studio-actions button')) button.disabled = true;
        }
    }

    newButton.addEventListener('click', () => openComposer());
    loginButton.addEventListener('click', openLogin);
    hiddenButton.addEventListener('click', () => { renderHidden(); hiddenDialog.showModal(); });
    document.querySelector('#studio-profile').addEventListener('click', openProfile);
    document.querySelector('#studio-logout').addEventListener('click', async () => {
        try { await request('/api/auth/logout', { method: 'POST', body: {} }); reloadWithNotice('Logged out.'); }
        catch (error) { say(error.message); }
    });
    for (const close of document.querySelectorAll('[data-close-dialog]')) {
        close.addEventListener('click', () => document.getElementById(close.dataset.closeDialog).close());
    }
    authForm.addEventListener('submit', async event => {
        event.preventDefault();
        const button = document.querySelector('#studio-auth-submit');
        const status = document.querySelector('#studio-auth-message');
        button.disabled = true;
        status.textContent = session.setup_required ? 'Creating your login…' : 'Logging in…';
        try {
            await request(session.setup_required ? '/api/auth/setup' : '/api/auth/login', {
                method: 'POST', body: { password: authForm.elements.password.value, user_id: authForm.elements.user_id.value }
            });
            authForm.elements.password.value = '';
            reloadWithNotice('You are logged in.');
        } catch (error) { status.textContent = error.message; }
        finally { button.disabled = false; }
    });
    profileForm.elements.avatar.addEventListener('change', () => {
        if (profileAvatarURL) URL.revokeObjectURL(profileAvatarURL);
        const file = profileForm.elements.avatar.files[0];
        profileAvatarURL = file ? URL.createObjectURL(file) : null;
        document.querySelector('#studio-profile-avatar').src = profileAvatarURL || session.user.avatar || '/default-avatar.png';
    });
    async function saveProfile(event, creating) {
        event.preventDefault();
        const currentForm = event.currentTarget;
        const button = currentForm.querySelector('[type=submit]');
        const status = document.querySelector(creating ? '#studio-add-user-message' : '#studio-profile-message');
        const file = currentForm.elements.avatar.files[0];
        if (file && file.size > 10 * 1024 * 1024) { status.textContent = 'Your avatar must be 10 MB or smaller.'; return; }
        const payload = { name: currentForm.elements.name.value, bio: currentForm.elements.bio.value };
        if (creating) { payload.id = currentForm.elements.id.value; payload.password = currentForm.elements.password.value; }
        const data = new FormData();
        data.append('post', JSON.stringify(payload));
        if (file) data.append('pictures', file);
        button.disabled = true;
        status.textContent = creating ? 'Creating user…' : 'Saving profile…';
        try {
            const result = await request('/api/users' + (creating ? '' : `/${session.user.id}`), { method: creating ? 'POST' : 'PUT', body: data });
            if (creating) currentForm.elements.password.value = '';
            if (result.build_error) {
                if (!creating) session.user = result.user;
                status.textContent = `Saved. The preview needs a correction: ${result.build_error}`;
            } else reloadWithNotice(creating ? `User ${result.user.name} added. They can now log in.` : 'Profile saved.');
        } catch (error) { status.textContent = error.message; }
        finally { button.disabled = false; }
    }
    profileForm.addEventListener('submit', event => saveProfile(event, false));
    addUserForm.addEventListener('submit', event => saveProfile(event, true));
    document.querySelector('#studio-close').addEventListener('click', closeComposer);
    composer.addEventListener('cancel', event => { event.preventDefault(); closeComposer(); });
    form.addEventListener('input', () => { dirty = true; });
    form.elements.date.addEventListener('input', saveLocation);
    document.querySelector('#studio-files').addEventListener('change', event => { addPhotos(event.target.files); event.target.value = ''; });
    const dropzone = document.querySelector('.studio-upload');
    dropzone.addEventListener('dragover', event => { event.preventDefault(); dropzone.classList.add('studio-upload-active'); });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('studio-upload-active'));
    dropzone.addEventListener('drop', event => { event.preventDefault(); dropzone.classList.remove('studio-upload-active'); addPhotos(event.dataTransfer.files); });

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (submitting) return;
        submitting = true;
        for (const button of form.querySelectorAll('[type=submit]')) button.disabled = true;
        message.textContent = 'Saving your moment…';
        try {
            const payload = {
                body: form.elements.body.value, date: form.elements.date.value + ':00+08:00',
                tags: form.elements.tags.value.split(',').map(tag => tag.trim()).filter(Boolean),
                note: form.elements.note.value, link: form.elements.link.value,
                link_text: form.elements.link_text.value, top: Number(form.elements.top.value || 0),
                pictures: retainedPhotos, draft: event.submitter?.value === 'draft', revision: editing?.revision
            };
            const data = new FormData();
            data.append('post', JSON.stringify(payload));
            for (const upload of uploads) data.append('pictures', upload.file);
            const path = '/api/moments' + (editing ? `/${editing.id}` : '');
            const result = await request(path, { method: editing ? 'PUT' : 'POST', body: data });
            dirty = false;
            if (result.build_error) {
                editing = result.moment;
                retainedPhotos = [...editing.pictures];
                for (const upload of uploads) URL.revokeObjectURL(upload.url);
                uploads = [];
                renderPhotos();
                message.textContent = `Your moment is saved, but its preview needs a correction: ${result.build_error}`;
                document.querySelector('#studio-publish').textContent = 'Save changes';
            } else location.assign('/');
        } catch (error) { message.textContent = error.message; }
        finally {
            submitting = false;
            for (const button of form.querySelectorAll('[type=submit]')) button.disabled = false;
        }
    });

    window.addEventListener('beforeunload', event => {
        const unfinishedComment = rows.some(row => row.querySelector('.studio-comment-form textarea')?.value.trim());
        if (dirty || submitting || unfinishedComment) { event.preventDefault(); event.returnValue = ''; }
    });
    setInterval(async () => {
        if (!session || [composer, hiddenDialog, authDialog, profileDialog].some(dialog => dialog.open) || rows.some(row => row.querySelector('.studio-comment-form textarea')?.value.trim())) return;
        try {
            const current = await request('/api/session');
            if ((current.generation !== session.generation && !current.build_error) || current.authenticated !== session.authenticated || current.user?.id !== session.user?.id) location.reload();
        } catch (_) { /* Keep unsaved text intact when the editor is offline. */ }
    }, 4000);
    connect();
})();
