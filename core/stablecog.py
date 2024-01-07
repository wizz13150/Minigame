import base64
import contextlib
import discord
import io
import random
import requests
import time
import traceback
import asyncio
import threading
from urllib.parse import quote
from PIL import Image, ImageFilter, ImageEnhance, PngImagePlugin
from discord import option
from discord.ext import commands
from typing import Optional

from core import utility
from core import queuehandler
from core import viewhandler
from core import settings

# a list of parameters, used to sanatize text
dream_params = [
    'prompt',
    'negative',
    'checkpoint',
    'steps',
    'width',
    'height',
    'guidance_scale',
    'sampler',
    'seed',
    'strength',
    'init_url',
    'batch',
    'style',
    'facefix',
    'tiling',
    'highres_fix',
    'highres_fix_prompt',
    'highres_fix_negative',
    'clip_skip',
    'hypernet',
    'controlnet_model',
    'controlnet_url',
    'controlnet_weight',
    'script'
]

scripts = [
    'inpaint alphamask',
    'outpaint center',
    'outpaint up',
    'outpaint down',
    'outpaint left',
    'outpaint right',
    'preset steps',
    'preset guidance_scale',
    'preset clip_skip',
    'spectrogram from image',
    'increment steps +1',
    'increment steps +2',
    'increment steps +3',
    'increment steps +4',
    'increment steps +5',
    'increment steps +10',
    'increment guidance_scale +0.1',
    'increment guidance_scale +0.5',
    'increment guidance_scale +1',
    'increment guidance_scale +2',
    'increment guidance_scale +3',
    'increment guidance_scale +4',
    'increment clip_skip +1'
]

class StableCog(commands.Cog, description='Create images from natural language.'):
    ctx_parse = discord.ApplicationContext
    def __init__(self, bot):
        self.wait_message: list[str] = []
        self.bot: discord.Bot = bot

    # list for scripts
    def autocomplete_scripts(self: discord.AutocompleteContext):
        return settings.custom_autocomplete(self, [
            script for script in scripts
        ])

    async def dream_handler(self, ctx: discord.ApplicationContext | discord.Message | discord.Interaction, *,
                            prompt: str,
                            negative: str = None,
                            checkpoint: Optional[str] = None,
                            steps: Optional[int] = None,
                            width: Optional[int] = None,
                            height: Optional[int] = None,
                            guidance_scale: Optional[float] = None,
                            sampler: Optional[str] = None,
                            seed: Optional[int] = None,
                            batch: Optional[int] = None,
                            style: Optional[str] = None,
                            facefix: Optional[str] = None,
                            tiling: Optional[bool] = False,
                            highres_fix: Optional[str] = None,
                            highres_fix_prompt: Optional[str] = None,
                            highres_fix_negative: Optional[str] = None,
                            clip_skip: Optional[int] = None,
                            init_image: Optional[discord.Attachment] = None,
                            init_url: Optional[str] = None,
                            strength: Optional[float] = None,
                            controlnet_model: Optional[str] = None,
                            controlnet_image: Optional[discord.Attachment] = None,
                            controlnet_url: Optional[str] = None,
                            controlnet_weight: Optional[float] = None,
                            script: Optional[str] = None):
        loop = asyncio.get_event_loop()
        guild = utility.get_guild(ctx)
        user = utility.get_user(ctx)
        content = None
        ephemeral = False
        append_options = ''

        try:
            print(f'Dream Request -- {user.name}#{user.discriminator} -- {guild}')

            # sanatize input strings
            def sanatize(input: str):
                if input:
                    input = input.replace('`', ' ')
                    input = input.replace('\n', ' ')
                    for param in dream_params:
                        input = input.replace(f' {param}:', f' {param} ')
                    input = input.strip()
                return input

            prompt = sanatize(prompt)
            negative = sanatize(negative)
            style = sanatize(style)
            init_url = sanatize(init_url)

            # try to make prompt more interesting
            if prompt.startswith('?'):
                try:
                    query = prompt.removeprefix('?')
                    if query == '':
                        query = random.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ')
                    else:
                        query = quote(query.strip())

                    def get_request():
                        try:
                            return requests.get(f'https://lexica.art/api/v1/search?q={query}', timeout=5)
                        except Exception as e:
                            return e

                    response = await loop.run_in_executor(None, get_request)
                    if type(response) is not requests.Response:
                        raise response

                    images = response.json()['images']
                    random_image = images[random.randrange(0, len(images))]
                    prompt = sanatize(random_image['prompt'])

                except Exception as e:
                    print(f'Dream rejected: Random prompt query failed.\n{e}\n{traceback.print_exc()}')
                    content = f'<@{user.id}> Random prompt query failed.'
                    ephemeral = True
                    raise Exception()

            # update defaults with any new defaults from settingscog
            if not checkpoint:
                checkpoint = settings.read(guild)['data_model']
            if negative == None:
                negative = settings.read(guild)['negative_prompt']
            if steps == None:
                steps = settings.read(guild)['default_steps']
            if guidance_scale == None:
                guidance_scale = settings.read(guild)['default_guidance_scale']
            if strength == None:
                if script and script.startswith('outpaint'):
                    strength = 1.0
                elif highres_fix != None and highres_fix != 'None':
                    strength = settings.read(guild)['default_strength_highres_fix']
                else:
                    strength = settings.read(guild)['default_strength']
            if batch is None:
                batch = settings.read(guild)['default_count']
            if sampler == None:
                sampler = settings.read(guild)['sampler']
            if clip_skip == None:
                clip_skip = settings.read(guild)['clip_skip']
            if style == 'None':
                style = None
            if facefix == 'None':
                facefix = None
            if highres_fix == 'None':
                highres_fix = None
            if highres_fix_prompt == None or highres_fix_prompt == 'None':
                highres_fix_prompt = ''
            if highres_fix_negative == None or highres_fix_negative == 'None':
                highres_fix_negative = ''
            if init_url == 'None':
                init_url = None
            if script == 'None':
                script = None
            is_resolution_set = width != None or height != None

            # remove highres fix if there is an init image
            if highres_fix != None and (init_url != None or init_image != None):
                append_options += f'\nHighres fix is not supported with init_image or init_url. I will remove highres fix.'
                highres_fix = None
                highres_fix_prompt = None
                highres_fix_negative = None

            # get data model and token from checkpoint
            data_model: str = None
            token: str = None
            for (display_name, full_name) in settings.global_var.model_names.items():
                if display_name == checkpoint or full_name == checkpoint:
                    checkpoint = display_name
                    data_model = full_name
                    token = settings.global_var.model_tokens[display_name]
                    if width == None:
                        width = settings.global_var.model_resolutions[display_name]
                    if height == None:
                        height = settings.global_var.model_resolutions[display_name]
                    break

            if data_model == None or data_model == '':
                print(f'Dream rejected: No checkpoint found.')
                content = f'<@{user.id}> Invalid checkpoint. I\'m not sure how this happened.'
                ephemeral = True
                raise Exception()

            if (not init_image and not init_url) and ('_inpaint' in checkpoint or '_inpaint' in data_model or '_refiner' in checkpoint or '_refiner' in data_model):
                model_name_new = settings.get_non_inpaint_model(checkpoint)
                if model_name_new:
                    checkpoint = model_name_new
                    data_model = settings.global_var.model_names[model_name_new]
                    token = settings.global_var.model_tokens[model_name_new]
                    append_options += f'\nInpaint checkpoint was selected without init_image or init_url. I will use ``{checkpoint}`` instead'
                else:
                    print(f'Dream rejected: Inpaint model selected without init image.')
                    content = f'<@{user.id}> Invalid checkpoint. Inpainting model requires init_image or init_url.'
                    ephemeral = True
                    raise Exception()

            # validate autocomplete
            if highres_fix != None and highres_fix != 'None':
                if highres_fix not in settings.global_var.highres_upscaler_names:
                    highres_fix = None
                    append_options += '\nHigh-res fix not found. I will remove the high-res fix.'

            if style != None and style != 'None':
                if style not in settings.global_var.style_names:
                    style = None
                    append_options += '\nStyle not found. I will remove the style.'

            if script != None and script != 'None':
                if script not in scripts:
                    script = None
                    append_options += '\nScript not found. I will remove the script.'

            if seed == None: seed = random.randint(0, 0xFFFFFFFF)

            controlnet_preprocessor: str = None
            controlnet_data_model: str = None

            # get arguments that can be passed into the draw object
            def get_draw_object_args():
                return (self, ctx, prompt, negative, checkpoint, data_model,
                        steps, width, height, guidance_scale, sampler, seed,
                        strength, init_url, batch, style, facefix, tiling,
                        highres_fix, highres_fix_prompt, highres_fix_negative, clip_skip, script,
                        controlnet_model, controlnet_preprocessor, controlnet_data_model, controlnet_url, controlnet_weight)

            # get estimate of the compute cost of this dream
            def get_dream_cost(_width: int, _height: int, _steps: int, _count: int = 1):
                args = get_draw_object_args()
                dream_cost_draw_object = utility.DrawObject(*args)
                dream_cost_draw_object.width = _width
                dream_cost_draw_object.height = _height
                dream_cost_draw_object.steps = _steps
                dream_cost_draw_object.batch = _count
                return queuehandler.dream_queue.get_dream_cost(dream_cost_draw_object)
            dream_compute_cost = get_dream_cost(width, height, steps, 1)

            # get settings
            setting_max_compute = settings.read(guild)['max_compute']
            setting_max_compute_batch = settings.read(guild)['max_compute_batch']
            setting_max_steps = settings.read(guild)['max_steps']

            # apply script modifications
            increment_seed = 0
            increment_steps = 0
            increment_guidance_scale = 0
            increment_clip_skip = 0

            match script:
                case None:
                    increment_seed = 1

                case 'None':
                    increment_seed = 1

                case 'preset steps':
                    steps = 10
                    increment_steps = 5
                    batch = 9

                    average_step_cost = get_dream_cost(width, height, steps + (increment_steps * batch * 0.5), batch)
                    if average_step_cost > setting_max_compute_batch:
                        increment_steps = 10
                        batch = 5

                case 'preset guidance_scale':
                    guidance_scale = 5.0
                    increment_guidance_scale = 1.0
                    batch = max(10, batch)

                    if dream_compute_cost * batch > setting_max_compute_batch:
                        batch = int(batch / 2)
                        increment_guidance_scale = 2.0

                case 'preset clip_skip':
                    clip_skip = 1
                    increment_clip_skip = 1
                    batch = max(6, min(12, batch))

                case 'inpaint alphamask':
                    increment_seed = 1
                    if not init_image and not init_url:
                        print(f'Dream rejected: Init image not found.')
                        content = 'Inpainting requires init_image or init_url! I use the alpha channel (transparency) as the inpainting mask.'
                        ephemeral = True
                        raise Exception()

                case other:
                    try:
                        script_parts = script.split(' ')
                        script_setting = script_parts[0]
                        if len(script_parts) > 1: script_param = script_parts[1]
                        if len(script_parts) > 2: script_value = float(script_parts[2])

                        if script_setting == 'increment' and script_param and script_value:
                            match script_param:
                                case 'steps':
                                    increment_steps = int(script_value)
                                    batch = max(5, batch)
                                case 'guidance_scale':
                                    increment_guidance_scale = script_value
                                    if increment_guidance_scale < 1.0:
                                        batch = max(10, batch)
                                    else:
                                        batch = max(4, batch)
                                case 'clip_skip':
                                    increment_clip_skip = int(script_value)
                                    batch = max(4, batch)
                                    clip_skip_max = clip_skip + (batch * increment_clip_skip)
                                    if clip_skip_max > 12:
                                        batch = clip_skip_max - 12

                        elif script_setting == 'outpaint':
                            increment_seed = 1
                            if not init_image and not init_url:
                                print(f'Dream rejected: Init image not found.')
                                content = 'Outpainting requires init_image or init_url!'
                                ephemeral = True
                                raise Exception()

                        else:
                            raise Exception()

                    except:
                        if content == None:
                            append_options += '\nInvalid script. I will ignore the script parameter.'
                        else:
                            raise Exception()
                        increment_seed = 1
                        increment_steps = 0
                        increment_guidance_scale = 0
                        increment_clip_skip = 0

            # lower step value to the highest setting if user goes over max steps
            if dream_compute_cost > setting_max_compute:
                steps = min(int(float(steps) * (setting_max_compute / dream_compute_cost)), setting_max_steps)
                append_options += '\nDream compute cost is too high! Steps reduced to ``' + str(steps) + '``'
            if steps > setting_max_steps:
                steps = setting_max_steps
                append_options += '\nExceeded maximum of ``' + str(steps) + '`` steps! This is the best I can do...'

            # reduce batch count if batch compute cost is too high
            if batch != 1:
                if increment_steps:
                    dream_compute_batch_cost = get_dream_cost(width, height, steps + (increment_steps * batch * 0.5), batch)
                else:
                    dream_compute_batch_cost = get_dream_cost(width, height, steps, batch)
                setting_max_count = settings.read(guild)['max_count']
                if dream_compute_batch_cost > setting_max_compute_batch:
                    batch = min(int(float(batch) * setting_max_compute_batch / dream_compute_batch_cost), setting_max_count)
                    append_options += '\nBatch compute cost is too high! Batch count reduced to ``' + str(batch) + '``'
                if batch > setting_max_count:
                    batch = setting_max_count
                    append_options += '\nExceeded maximum of ``' + str(batch) + '`` images! This is the best I can do...'

            # calculate total cost of queued items and reject if there is too expensive
            dream_cost = round(get_dream_cost(width, height, steps, batch), 2)
            queue_cost = round(queuehandler.dream_queue.get_user_queue_cost(user.id), 2)
            print(f'Estimated total compute cost -- Dream: {dream_cost} Queue: {queue_cost} Total: {dream_cost + queue_cost}')

            if dream_cost + queue_cost > settings.read(guild)['max_compute_queue']:
                print(f'Dream rejected: Too much in queue already')
                content = f'<@{user.id}> Please wait! You have too much queued up.'
                ephemeral = True
                raise Exception()

            # get input image
            image: str = None
            mask: str = None
            image_validated = True
            if init_url or init_image:
                if not init_url and init_image:
                    init_url = init_image.url

                if init_url.startswith('https://cdn.discordapp.com/') == False and init_url.startswith('https://media.discordapp.net/') == False:
                    print(f'Dream rejected: Image is not from the Discord CDN.')
                    content = 'Only URL images from the Discord CDN are allowed!'
                    ephemeral = True
                    raise Exception()

                try:
                    # reject URL downloads larger than 10MB
                    url_head = await loop.run_in_executor(None, requests.head, init_url)
                    url_size = int(url_head.headers.get('content-length', -1))
                except:
                    content = 'Image not found! Please check the image URL.'
                    ephemeral = True
                    raise Exception()

                # check image download size
                if url_size > 10 * 1024 * 1024:
                    print(f'Dream rejected: Image download too large.')
                    content = 'Image download is too large! Please make the download size smaller.'
                    ephemeral = True
                    raise Exception()

                # defer response before downloading
                try:
                    loop.create_task(ctx.defer())
                except:
                    pass

                # download and encode the image
                try:
                    image_response = await loop.run_in_executor(None, requests.get, init_url)
                    image_data = image_response.content
                    image_string = base64.b64encode(image_data).decode('utf-8')
                except:
                    print(f'Dream rejected: Image download failed.')
                    content = 'Image download failed! Please check the image URL.'
                    ephemeral = True
                    raise Exception()

                # check if image can open
                try:
                    image_bytes = io.BytesIO(image_data)
                    image_pil = Image.open(image_bytes)
                    image_pil_width, image_pil_height = image_pil.size
                except Exception as e:
                    print(f'Dream rejected: Image is corrupted.')
                    print(f'\n{traceback.print_exc()}')
                    content = 'Image is corrupted! Please check the image you uploaded.'
                    ephemeral = True
                    raise Exception()

                # limit image width/height
                if image_pil_width * image_pil_height > 4096 * 4096:
                    print(f'Dream rejected: Image size is too large.')
                    content = 'Image size is too large! Please use a lower resolution image.'
                    ephemeral = True
                    raise Exception()

                # fix output aspect ratio
                if is_resolution_set == False:
                    target_aspect_ratio = float(image_pil_width) / float(image_pil_height)
                    if target_aspect_ratio > 1.01:
                        height = int(round(float(width) / target_aspect_ratio / 64.0) * 64)
                    if target_aspect_ratio < 0.99:
                        width = int(round(float(height) * target_aspect_ratio / 64.0) * 64)

                    print(f'{is_resolution_set} {target_aspect_ratio} {image_pil_width}x{image_pil_height} {width}x{height}')

                # setup image variable
                image = 'data:image/png;base64,' + image_string
                image_validated = True

                # setup inpainting mask
                if script != None and script != 'None':
                    script_parts = script.split(' ')
                    script_setting = script_parts[0]
                    script_param = script_parts[1]

                    def setup_inpaint_mask(outpaint: bool = False):
                        # get mask from alpha channel
                        image_r, image_g, image_b, image_a = image_pil.split()
                        if outpaint:
                            image_a = image_a.filter(ImageFilter.BoxBlur(radius=64.0))
                            pixels = image_a.getdata()
                            modified_pixels = [((c - 127) * 2) for c in pixels]
                            image_a.putdata(modified_pixels)
                        mask_pil = Image.new('L', (image_pil_width, image_pil_height), 255)
                        mask_pil.paste(image_a, (0, 0, image_pil_width, image_pil_height))
                        buffer = io.BytesIO()
                        mask_pil.save(buffer, format='PNG')
                        mask_data = buffer.getvalue()
                        mask_string = base64.b64encode(mask_data).decode('utf-8')
                        return 'data:image/png;base64,' + mask_string

                    if script_setting == 'inpaint' and script_param == 'alphamask':
                        try:
                            mask = await loop.run_in_executor(None, setup_inpaint_mask)
                        except:
                            print(f'Dream rejected: Alpha mask separation failed.')
                            content = ('Could not separate alpha mask! Please check the image you uploaded.\n'
                                       'I use the alpha channel (transparency) as the inpainting mask.')
                            ephemeral = True
                            raise Exception()

                    elif script_setting == 'outpaint':
                        def setup_outpaint():
                            # get border width and height for outpainting
                            border_width = int(float(image_pil_width) * 0.125)
                            border_height = int(float(image_pil_height) * 0.125)

                            # resize init image to allow room for borders
                            resized_image = image_pil.resize((image_pil_width - border_width * 2, image_pil_height - border_height * 2))

                            # create a new image of the original size and paste the resized image into it
                            new_image_pil = Image.new('RGBA', (image_pil_width, image_pil_height), (127, 127, 127, 0))
                            match script_param:
                                case 'center':
                                    posX = border_width
                                    posY = border_height
                                case 'up':
                                    posX = border_width
                                    posY = min(border_height * 2, image_pil_height - resized_image.height)
                                case 'down':
                                    posX = border_width
                                    posY = 1
                                case 'left':
                                    posX = min(border_width * 2, image_pil_width - resized_image.width)
                                    posY = border_height
                                case 'right':
                                    posX = 1
                                    posY = border_height
                                case other:
                                    raise Exception()

                            new_image_pil.paste(resized_image, (posX, posY, posX + resized_image.width, posY + resized_image.height))

                            # generate output image
                            buffer = io.BytesIO()
                            new_image_pil.save(buffer, format='PNG')
                            new_image_data = buffer.getvalue()
                            new_image_string = base64.b64encode(new_image_data).decode('utf-8')

                            return new_image_pil, 'data:image/png;base64,' + new_image_string
                        try:
                            image_pil, image = await loop.run_in_executor(None, setup_outpaint)
                            mask = await loop.run_in_executor(None, setup_inpaint_mask, True)
                        except Exception as e:
                            print(f'Dream rejected: Alpha mask separation failed.')
                            content = 'Could not setup outpaint image! Please check the image you uploaded'
                            ephemeral = True
                            raise Exception()


            if image_validated == False:
                raise Exception()


             # get input controlnet image
            controlnet_image_data: str = None
            # controlnet_image_mask: str = None
            controlnet_validated = True
            controlnet_found = False

            # validate controlnet model
            if controlnet_model == 'None':
                controlnet_model = None
            if controlnet_url == 'None':
                controlnet_url = None

            if controlnet_model != None and controlnet_model != 'None':
                for (display_name, controlnet_preprocessor) in settings.global_var.controlnet_models_preprocessor.items():
                    if controlnet_model == display_name or controlnet_model == controlnet_preprocessor:
                        controlnet_model = display_name
                        controlnet_preprocessor = controlnet_preprocessor
                        controlnet_data_model = settings.global_var.controlnet_models[display_name]
                        controlnet_found = True
                        break

                if controlnet_found == False:
                    controlnet_model = None
                    controlnet_preprocessor = None
                    controlnet_data_model = None

                    if not (controlnet_url or controlnet_image):
                        append_options += '\nControlnet model not found. I will remove the controlnet.'

            # set default control model if only controlnet image is specified
            if controlnet_found == False:
                if controlnet_url or controlnet_image:
                    for (display_name, controlnet_preprocessor) in settings.global_var.controlnet_models_preprocessor.items():
                        if display_name == 'depth':
                            controlnet_model = display_name
                            controlnet_preprocessor = controlnet_preprocessor
                            controlnet_data_model = settings.global_var.controlnet_models[display_name]
                            controlnet_found = True
                            break

                    # if controlnet_found == False:
                    #     controlnet_model, controlnet_data_model = settings.global_var.controlnet_models.items()
                    #     append_options += f'\nControlnet model unspecified. I will use ``{controlnet_model}``.'

            if controlnet_found:
                if controlnet_url or controlnet_image:
                    if not controlnet_url and controlnet_image:
                        controlnet_url = controlnet_image.url

                    if controlnet_url.startswith('https://cdn.discordapp.com/') == False and controlnet_url.startswith('https://media.discordapp.net/') == False:
                        print(f'Dream rejected: Controlnet image is not from the Discord CDN.')
                        content = 'Only URL images from the Discord CDN are allowed!'
                        ephemeral = True
                        raise Exception()

                    try:
                        # reject URL downloads larger than 10MB
                        url_head = await loop.run_in_executor(None, requests.head, controlnet_url)
                        url_size = int(url_head.headers.get('content-length', -1))
                    except:
                        content = 'Controlnet image not found! Please check the image URL.'
                        ephemeral = True
                        raise Exception()

                    # check image download size
                    if url_size > 10 * 1024 * 1024:
                        print(f'Dream rejected: Controlnet image download too large.')
                        content = 'Controlnet image download is too large! Please make the download size smaller.'
                        ephemeral = True
                        raise Exception()

                    # download and encode the image
                    try:
                        image_response = await loop.run_in_executor(None, requests.get, controlnet_url)
                        image_data = image_response.content
                        image_string = base64.b64encode(image_data).decode('utf-8')
                    except:
                        print(f'Dream rejected: Controlnet image download failed.')
                        content = 'Controlnet image download failed! Please check the image URL.'
                        ephemeral = True
                        raise Exception()

                    # check if image can open
                    try:
                        image_bytes = io.BytesIO(image_data)
                        image_pil = Image.open(image_bytes)
                        image_pil_width, image_pil_height = image_pil.size
                    except Exception as e:
                        print(f'Dream rejected: Controlnet image is corrupted.')
                        print(f'\n{traceback.print_exc()}')
                        content = 'Controlnet image is corrupted! Please check the image you uploaded.'
                        ephemeral = True
                        raise Exception()

                    # limit image width/height
                    if image_pil_width * image_pil_height > 4096 * 4096:
                        print(f'Dream rejected: Controlnet image size is too large.')
                        content = 'Controlnet image size is too large! Please use a lower resolution image.'
                        ephemeral = True
                        raise Exception()

                    # setup image mask
                    # image_mask = Image.new('RGB', (width, height), color='black')
                    # buffer = io.BytesIO()
                    # image_mask.save(buffer, format='PNG')
                    # controlnet_image_mask = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    # controlnet_image_mask = 'data:image/png;base64,' + controlnet_image_mask

                    # setup image variable
                    # controlnet_image_data = 'data:image/png;base64,' + image_string
                    controlnet_image_data = image_string
                    controlnet_validated = True

                # fallback to using init_image
                elif image_validated and image:
                    controlnet_image_data = image.removeprefix('data:image/png;base64,')
                    controlnet_validated = True

                else:
                    print(f'Dream rejected: Controlnet image not found.')
                    print(f'\n{traceback.print_exc()}')
                    content = 'Controlnet image not found! An init or controlnet image is required when using a controlnet model.'
                    ephemeral = True
                    raise Exception()

                # validate controlnet weight
                if controlnet_weight == None:
                    controlnet_weight = 0.75
                else:
                    controlnet_weight = max(0, min(2, controlnet_weight))


            if controlnet_validated == False:
                raise Exception()


            # increment number of images generated
            settings.increment_stats(batch)

            # create draw object
            def get_draw_object(message: str = None):
                args = get_draw_object_args()
                queue_object = utility.DrawObject(*args)

                # create view to handle buttons
                queue_object.view = viewhandler.DrawView(queue_object)

                # send message with queue object
                if message == None:
                    queue_object.message = queue_object.get_command()
                    print(queue_object.message) # log the command
                else:
                    queue_object.message = message

                # construct a payload
                payload_prompt = queue_object.prompt
                if token: payload_prompt = f'{token} {payload_prompt}'

                payload_negative = queue_object.negative

                payload_hires_prompt = queue_object.highres_fix_prompt
                if token: payload_hires_prompt = f'{token} {payload_hires_prompt}'

                payload_hires_negative = queue_object.highres_fix_negative

                # update prompt if style is used
                if queue_object.style and queue_object.style != 'None' and queue_object.style in settings.global_var.style_names:
                    # payload.update({
                    #     'styles': [queue_object.style]
                    # })
                    values: list[str] = settings.global_var.style_names[queue_object.style].split('\n')
                    style_prompt = values[0]
                    style_negative = values[1]

                    if style_prompt:
                        if payload_prompt:
                            payload_prompt += ', ' + style_prompt
                        else:
                            payload_prompt = style_prompt

                        if payload_hires_prompt:
                            payload_hires_prompt += ', ' + style_prompt
                        else:
                            payload_hires_prompt = style_prompt

                    if style_negative:
                        if payload_negative:
                            payload_negative += ', ' + style_negative
                        else:
                            payload_negative = style_negative

                        if payload_hires_negative:
                            payload_hires_negative += ', ' + style_negative
                        else:
                            payload_hires_negative = style_negative

                payload = {
                    'prompt': payload_prompt,
                    'negative_prompt': payload_negative,
                    'steps': queue_object.steps,
                    'width': queue_object.width,
                    'height': queue_object.height,
                    'cfg_scale': queue_object.guidance_scale,
                    'sampler_index': queue_object.sampler,
                    'seed': queue_object.seed,
                    'seed_resize_from_h': 0,
                    'seed_resize_from_w': 0,
                    'denoising_strength': None,
                    'tiling': queue_object.tiling,
                    'n_iter': 1
                }

                # update payload if init_img or init_url is used
                if queue_object.init_url:
                    payload.update({
                        'init_images': [image],
                        'denoising_strength': queue_object.strength
                    })

                    if mask:
                        payload.update({
                            'mask': mask,
                            'mask_blur': 0,
                            'inpainting_fill': 0,
                            'inpaint_full_res': False,
                            'inpaint_full_res_padding': 0,
                            'inpainting_mask_invert': 1,
                        })

                # update payload if high-res fix is used
                if queue_object.highres_fix != None and queue_object.highres_fix != 'None':
                    payload.update({
                        'width': int(queue_object.width / 2),
                        'height': int(queue_object.height / 2),
                        "enable_hr": True,
                        "hr_upscaler": queue_object.highres_fix,
                        "hr_scale": 2,
                        "hr_second_pass_steps": int(float(queue_object.steps) * queue_object.strength),
                        "denoising_strength": queue_object.strength
                    })

                    # update highres prompts if style is used
                    if queue_object.style and queue_object.style != 'None' and queue_object.style in settings.global_var.style_names:
                        # payload.update({
                        #     'styles': [queue_object.style]
                        # })
                        values: list[str] = settings.global_var.style_names[queue_object.style].split('\n')
                        style_prompt = values[0]
                        style_negative = values[1]

                    # construct payloads for highres prompts
                    if queue_object.highres_fix_prompt != None and queue_object.highres_fix_prompt != '':
                        payload.update({
                            "hr_prompt": payload_hires_prompt
                        })

                    if queue_object.highres_fix_negative != None and queue_object.highres_fix_negative != '':
                        payload.update({
                            "hr_negative_prompt": payload_hires_negative
                        })

                # add any options that would go into the override_settings
                override_settings = {
                    # 'sd_model_checkpoint': queue_object.data_model
                    'CLIP_stop_at_last_layers': queue_object.clip_skip
                }

                # update payload if facefix is used
                if queue_object.facefix != None and queue_object.facefix != 'None':
                    payload.update({
                        'restore_faces': True,
                    })
                    override_settings['face_restoration_model'] = queue_object.facefix

                # update payload with override_settings
                override_payload = {
                    'override_settings': override_settings
                }
                payload.update(override_payload)

                # setup controlnet payload
                if queue_object.controlnet_model and controlnet_validated:
                    controlnet_payload = {
                        'input_image': [controlnet_image_data],
                        # 'mask': [controlnet_image_mask],
                        'module': queue_object.controlnet_preprocessor,
                        'model': queue_object.controlnet_data_model,
                        'weight': controlnet_weight,
                        'resize_mode': 'Just Resize',
                        'lowvram': False,
                        'processor_res': min(queue_object.width, queue_object.height),
                        'threshold_a': 64,
                        'threshold_b': 64,
                    }
                    payload.update({
                        'alwayson_scripts': {
                            'controlnet': {
                                'args': [
                                    controlnet_payload
                                ]
                            }
                        }
                    })

                # attach payload to queue object
                queue_object.payload = payload
                return queue_object

            # start the dream
            priority = int(settings.read(guild)['priority'])
            if dream_cost + queue_cost > settings.read(guild)['max_compute']:
                priority += 2
            elif queue_cost > 0.0:
                priority += 1

            if batch == 1:
                queue_length = queuehandler.dream_queue.process_dream(get_draw_object(), priority)
            else:
                last_draw_object = get_draw_object()
                queue_length = queuehandler.dream_queue.process_dream(last_draw_object, priority)

                if queue_length != None:
                    batch_count = 1
                    while batch_count < batch:
                        batch_count += 1
                        message = f'#{batch_count}`` ``'

                        if increment_seed:
                            seed += increment_seed
                            message += f'seed:{seed}'

                        if increment_steps:
                            steps += increment_steps
                            message += f'steps:{steps}'

                        if increment_guidance_scale:
                            guidance_scale += increment_guidance_scale
                            guidance_scale = round(guidance_scale, 4)
                            message += f'guidance_scale:{guidance_scale}'

                        if increment_clip_skip:
                            clip_skip += increment_clip_skip
                            message += f'clip_skip:{clip_skip}'

                        draw_object = get_draw_object(message)
                        draw_object.wait_for_dream = last_draw_object
                        last_draw_object = draw_object
                        queuehandler.dream_queue.process_dream(draw_object, priority, False)

            if queue_length == None:
                content = f'<@{user.id}> Sorry, I cannot handle this request right now.'
                ephemeral = True
            else:
                content = f'<@{user.id}> {settings.global_var.messages[random.randrange(0, len(settings.global_var.messages))]} Queue: ``{queue_length}``'
                if batch > 1: content = content + f' - Batch: ``{batch}``'
                content = content + append_options

        except Exception as e:
            if content == None:
                content = f'<@{user.id}> Something went wrong.\n{e}'
                print(content + f'\n{traceback.print_exc()}')
                ephemeral = True

        if content:
            if ephemeral:
                delete_after = 30
            else:
                delete_after = 120

            if type(ctx) is discord.ApplicationContext:
                loop.create_task(ctx.send_response(content=content, ephemeral=ephemeral, delete_after=delete_after))
            elif type(ctx) is discord.Interaction:
                loop.create_task(ctx.response.send_message(content=content, ephemeral=ephemeral, delete_after=delete_after))
            elif type(ctx) is discord.Message:
                loop.create_task(ctx.reply(content, delete_after=delete_after))
            else:
                loop.create_task(ctx.channel.send(content, delete_after=delete_after))

    # generate the image
    def dream(self, queue_object: utility.DrawObject, web_ui: utility.WebUI, queue_continue: threading.Event):
        user = utility.get_user(queue_object.ctx)

        try:
            # get webui session
            s = web_ui.get_session()
            if s == None:
                # no session, return the object to the queue handler to try again
                queuehandler.dream_queue.process_dream(queue_object, 0, False)
                return

            # only send model payload if one is defined
            if queue_object.data_model:
                model_payload = {
                    'sd_model_checkpoint': queue_object.data_model,
                }
                s.post(url=f'{web_ui.url}/sdapi/v1/options', json=model_payload, timeout=120)

            # safe for global queue to continue
            def continue_queue():
                time.sleep(0.1)
                queue_continue.set()
            threading.Thread(target=continue_queue, daemon=True).start()

            if queue_object.init_url:
                url = f'{web_ui.url}/sdapi/v1/img2img'
            else:
                url = f'{web_ui.url}/sdapi/v1/txt2img'
            # if queue_object.controlnet_model != None and queue_object.controlnet_model != 'None':
            #     url = url.replace('/sdapi/v1/', '/controlnet/')
            response = s.post(url=url, json=queue_object.payload, timeout=120)
            queue_object.payload = None

            def post_dream():
                try:
                    response_data = response.json()
                    # create safe/sanitized filename
                    keep_chars = (' ', '.', '_')
                    file_name = ''.join(c for c in queue_object.prompt if c.isalnum() or c in keep_chars).rstrip()

                    # save local copy of image and prepare PIL images
                    pil_images: list[Image.Image] = []
                    for i, image_base64 in enumerate(response_data['images']):
                        image = Image.open(io.BytesIO(base64.b64decode(image_base64.split(',',1)[0])))
                        pil_images.append(image)

                        # save png with metadata
                        if settings.global_var.dir != '--no-output':
                            try:
                                epoch_time = int(time.time())
                                file_path = f'{settings.global_var.dir}/{epoch_time}-{queue_object.seed}-{file_name[0:120]}-{i}.png'

                                metadata = PngImagePlugin.PngInfo()
                                metadata.add_text('parameters', response_data['info'])
                                image.save(file_path, pnginfo=metadata)
                                print(f'Saved image: {file_path}')
                            except Exception as e:
                                print(f'Unable to save image: {file_path}\n{traceback.print_exc()}')
                        else:
                            print(f'Received image: {int(time.time())}-{queue_object.seed}-{file_name[0:120]}-{i}.png')

                    # post to discord
                    with contextlib.ExitStack() as stack:
                        buffer_handles = [stack.enter_context(io.BytesIO()) for _ in pil_images]

                        for (pil_image, buffer) in zip(pil_images, buffer_handles):
                            pil_image.save(buffer, 'PNG')
                            buffer.seek(0)

                        files = [discord.File(fp=buffer, filename=f'{queue_object.seed}-{i}.png') for (i, buffer) in enumerate(buffer_handles)]
                        queuehandler.upload_queue.process_upload(utility.UploadObject(queue_object=queue_object,
                           content=f'<@{user.id}> ``{queue_object.message}``', files=files, view=queue_object.view
                        ))
                        queue_object.view = None

                except Exception as e:
                    content = f'<@{user.id}> ``{queue_object.message}``\nSomething went wrong.\n{e}'
                    print(content + f'\n{traceback.print_exc()}')
                    queuehandler.upload_queue.process_upload(utility.UploadObject(queue_object=queue_object, content=content, delete_after=30))

            threading.Thread(target=post_dream, daemon=True).start()

        except requests.exceptions.RequestException as e:
            # connection error, return items to queue
            time.sleep(5.0)
            web_ui.reconnect()
            queuehandler.dream_queue.process_dream(queue_object, 0, False)
            return

        except Exception as e:
            content = f'<@{user.id}> ``{queue_object.message}``\nSomething went wrong.\n{e}'
            print(content + f'\n{traceback.print_exc()}')
            queuehandler.upload_queue.process_upload(utility.UploadObject(queue_object=queue_object, content=content, delete_after=30))

    async def dream_object(self, draw_object: utility.DrawObject):
        loop = asyncio.get_running_loop()
        loop.create_task(self.dream_handler(ctx=draw_object.ctx,
            prompt=draw_object.prompt,
            negative=draw_object.negative,
            checkpoint=draw_object.model_name,
            width=draw_object.width,
            height=draw_object.height,
            guidance_scale=draw_object.guidance_scale,
            steps=draw_object.steps,
            sampler=draw_object.sampler,
            seed=draw_object.seed,
            init_image=None,
            init_url=draw_object.init_url,
            strength=draw_object.strength,
            batch=draw_object.batch,
            style=draw_object.style,
            facefix=draw_object.facefix,
            tiling=draw_object.tiling,
            highres_fix=draw_object.highres_fix,
            highres_fix_prompt=draw_object.highres_fix_prompt,
            highres_fix_negative=draw_object.highres_fix_negative,
            clip_skip=draw_object.clip_skip,
            controlnet_model=draw_object.controlnet_model,
            controlnet_url=draw_object.controlnet_url,
            controlnet_weight=draw_object.controlnet_weight,
            script=draw_object.script
        ))

    # process dream from a command string
    async def dream_command(self, ctx: discord.ApplicationContext | discord.Message | discord.Interaction, command: str, randomize_seed = True):
        queue_object = self.get_draw_object_from_command(command)

        queue_object.ctx = ctx
        if randomize_seed:
            queue_object.seed = None

        loop = asyncio.get_event_loop()
        loop.create_task(self.dream_object(queue_object))

    # get draw object from a command string
    def get_draw_object_from_command(self, command: str):
        # format command for easier processing
        command = '\n\n ' + command + '\n\n'
        for param in dream_params:
            command = command.replace(f' {param}:', f'\n\n{param}\n')
        command = command.replace('``', '\n\n')

        def get_param(param: str, default: str = None):
            result = utility.find_between(command, f'\n{param}\n', '\n\n')
            result = result.strip()
            if result == '':
                return default
            else:
                return result.strip()

        # get all parameters and validate inputs
        prompt = get_param('prompt', '')

        negative = get_param('negative')

        checkpoint = get_param('checkpoint')
        # if checkpoint not in settings.global_var.model_names: checkpoint = 'Default'

        try:
            width = int(get_param('width'))
            if width not in [x for x in range(192, 1025, 64)]:
                width = int(width / 64) * 64
                if width not in [x for x in range(192, 1025, 64)]: width = 512
        except:
            width = None

        try:
            height = int(get_param('height'))
            if height not in [x for x in range(192, 1025, 64)]:
                height = int(height / 64) * 64
                if height not in [x for x in range(192, 1025, 64)]: height = 512
        except:
            height = None

        try:
            guidance_scale = float(get_param('guidance_scale'))
            guidance_scale = max(1.0, guidance_scale)
        except:
            guidance_scale = 7.0

        try:
            steps = int(get_param('steps'))
            steps = max(1, steps)
        except:
            steps = None

        try:
            sampler = get_param('sampler')
            if sampler not in settings.global_var.sampler_names: sampler = None
        except:
            sampler = None

        try:
            seed = int(get_param('seed'))
        except:
            seed = None

        try:
            strength = float(get_param('strength'))
            strength = max(0.0, min(1.0, strength))
        except:
            strength = None

        try:
            batch = int(get_param('batch'))
            batch = max(1, batch)
        except:
            batch = None

        init_url = get_param('init_url')
        if init_url == '':
            init_url = None

        style = get_param('style')
        # if style not in settings.global_var.style_names: style = None

        try:
            tiling = get_param('tiling')
            if tiling.lower() == 'true':
                tiling = True
            else:
                tiling = False
        except:
            tiling = False

        try:
            highres_fix = get_param('highres_fix')
        except:
            highres_fix = None

        highres_fix_prompt = get_param('highres_fix_prompt')

        highres_fix_negative = get_param('highres_fix_negative')

        try:
            facefix = get_param('facefix')
            if facefix not in settings.global_var.facefix_models: facefix = None
        except:
            facefix = None

        try:
            clip_skip = int(get_param('clip_skip'))
            clip_skip = max(1, min(12, clip_skip))
        except:
            clip_skip = None

        controlnet_model = get_param('controlnet_model')
        if controlnet_model == '':
            controlnet_model = None

        controlnet_url = get_param('controlnet_url')
        if controlnet_url == '':
            controlnet_url = None

        try:
            controlnet_weight = float(get_param('controlnet_weight'))
            controlnet_weight = max(0.0, min(2.0, controlnet_weight))
        except:
            controlnet_weight = None

        script = get_param('script')
        # if script not in self.scripts_autocomplete(): script = None

        return utility.DrawObject(
            cog=None,
            ctx=None,
            prompt=prompt,
            negative=negative,
            model_name=checkpoint,
            data_model=checkpoint,
            steps=steps,
            width=width,
            height=height,
            guidance_scale=guidance_scale,
            sampler=sampler,
            seed=seed,
            strength=strength,
            init_url=init_url,
            batch=batch,
            style=style,
            facefix=facefix,
            tiling=tiling,
            highres_fix=highres_fix,
            highres_fix_prompt=highres_fix_prompt,
            highres_fix_negative=highres_fix_negative,
            clip_skip=clip_skip,
            controlnet_model=controlnet_model,
            controlnet_url=controlnet_url,
            controlnet_weight=controlnet_weight,
            script=script
        )

def setup(bot: discord.Bot):
    bot.add_cog(StableCog(bot))
